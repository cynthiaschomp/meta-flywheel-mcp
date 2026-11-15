# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""HMAC-SHA256 webhook verifier, self-author circuit breaker, Threads reply, and Private Reply DM."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .auth import GRAPH_FB_BASE, GRAPH_THREADS_V1, make_live_reply_sender
from .db import FlywheelStore
from .urls import normalize_https_url

ReplySender = Callable[[str, Dict[str, Any]], Dict[str, Any]]
LeadCapturedHook = Callable[[Dict[str, Any]], None]

PUBLIC_NUDGES = (
    "Sent to your DMs—check Message Requests if we aren't connected yet!",
    "Just dropped the repo + walkthrough in your inbox! (Check Message Requests if we aren't connected yet!)",
    "On its way—sent to your DMs! Check Message Requests if you don't see it right away.",
)


def verify_hub_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify Meta X-Hub-Signature-256 HMAC-SHA256 digest in <1ms constant time."""
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    provided_hex = signature_header.split("=", 1)[1].strip().lower()
    if len(provided_hex) != 64:
        return False
    expected_hex = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, provided_hex)


def match_keyword(comment_text: str, keyword: str) -> bool:
    """Match normalized word-boundary trigger keyword ignoring punctuation and emojis."""
    kw = keyword.strip()
    if not kw or not comment_text:
        return False
    normalized_text = re.sub(r"[\u200d\ufe0f]+", "", comment_text)
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(kw)}(?![A-Za-z0-9_])", re.I)
    return bool(pattern.search(normalized_text))


def select_public_nudge(comment_id: str) -> str:
    """Deterministically rotate public comment replies including the Message Requests reminder."""
    digest = hashlib.sha256(comment_id.encode("utf-8")).digest()
    return PUBLIC_NUDGES[digest[0] % len(PUBLIC_NUDGES)]


def build_atomic_private_reply_payload(comment_id: str, repo_url: str, booking_url: str) -> Dict[str, Any]:
    """Build a single-bubble Private Reply DM payload combining repo_url + booking_url."""
    if not comment_id.strip():
        raise ValueError("comment_id is required for Private Reply DM.")
    norm_repo, norm_booking = normalize_https_url(repo_url), normalize_https_url(booking_url)
    dm_text = (
        f"Here is the open-source repo + walkthrough: {norm_repo}\n\n"
        f"If you want help applying it to your stack, grab a discovery slot here: {norm_booking}"
    )
    return {"recipient": {"comment_id": comment_id.strip()}, "message": {"text": dm_text}}


def _dry_run_sender(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    digest = hashlib.sha256(f"{endpoint}:{payload}".encode("utf-8")).hexdigest()[:8]
    return {"id": f"msg_{digest}", "endpoint": endpoint, "payload": payload}


def _normalize_actor_map(actor_ids: Optional[Mapping[str, str]], own_actor_ids: Optional[Iterable[str]]) -> tuple[Dict[str, str], set[str]]:
    amap: Dict[str, str] = {}
    if actor_ids:
        amap = {
            "fb_page": str(actor_ids.get("fb_page") or actor_ids.get("page_id") or "").strip(),
            "ig_pro": str(actor_ids.get("ig_pro") or actor_ids.get("ig_user_id") or "").strip(),
            "threads": str(actor_ids.get("threads") or actor_ids.get("threads_user_id") or "").strip(),
        }
    own = {v for v in amap.values() if v}
    if own_actor_ids is not None:
        own |= {str(x).strip() for x in own_actor_ids if str(x).strip()}
    return amap, own


def process_comment_webhook(
    *,
    store: FlywheelStore,
    campaign_id: str,
    platform: str,
    comment_id: str,
    author_id: str,
    comment_text: str,
    actor_ids: Optional[Mapping[str, str]] = None,
    own_actor_ids: Optional[Iterable[str]] = None,
    tokens: Optional[Mapping[str, str]] = None,
    raw_body: Optional[bytes] = None,
    signature_header: Optional[str] = None,
    app_secret: Optional[str] = None,
    reply_sender: Optional[ReplySender] = None,
    on_lead_captured: Optional[LeadCapturedHook] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Verify signature, enforce self-author breaker & idempotency, and route Threads vs FB/IG replies."""
    if raw_body is not None or signature_header is not None or app_secret is not None:
        if not verify_hub_signature(raw_body or b"", signature_header or "", app_secret or ""):
            return {"status": "rejected", "reason": "invalid_signature"}

    plat = platform.strip().lower()
    actor_clean = author_id.strip()
    amap, own_ids = _normalize_actor_map(actor_ids, own_actor_ids)
    if not actor_clean or actor_clean in own_ids:
        return {"status": "ignored", "reason": "self_author_circuit_breaker"}

    campaign = store.get_campaign(campaign_id)
    kw = campaign["keyword"]
    if not match_keyword(comment_text, kw):
        return {"status": "ignored", "reason": "keyword_mismatch"}

    claimed, token_or_reason = store.claim_inbound_comment(
        campaign_id=campaign_id, platform=plat, comment_id=comment_id.strip(),
        commenter_id=actor_clean, comment_text=comment_text, matched_keyword=kw,
    )
    if not claimed:
        return {"status": "duplicate", "reason": token_or_reason}

    event_id = token_or_reason
    sender = reply_sender or (_dry_run_sender if dry_run else make_live_reply_sender(tokens))
    subject_id = amap.get(plat) or "me"

    try:
        if plat == "threads":
            nudge_text = f"Here is the open-source repo: {campaign['repo_url']} · Book a walkthrough: {campaign['booking_url']}"
            th_payload = {"media_type": "TEXT", "reply_to_id": comment_id.strip(), "text": nudge_text}
            pub_res = sender(f"{GRAPH_THREADS_V1}/{subject_id}/threads", th_payload)
            dm_payload: Optional[Dict[str, Any]] = None
            dm_bubbles = 0
            store.record_comment_dispatch(event_id, str(pub_res.get("id", "")), "")
        else:
            nudge_text = select_public_nudge(comment_id)
            dm_payload = build_atomic_private_reply_payload(comment_id, campaign["repo_url"], campaign["booking_url"])
            pub_ep = f"{GRAPH_FB_BASE}/{comment_id.strip()}/replies" if plat == "ig_pro" else f"{GRAPH_FB_BASE}/{comment_id.strip()}/comments"
            pub_res = sender(pub_ep, {"message": nudge_text})
            dm_res = sender(f"{GRAPH_FB_BASE}/{subject_id}/messages", dm_payload)
            dm_bubbles = 1
            store.record_comment_dispatch(event_id, str(pub_res.get("id", "")), str(dm_res.get("id", "")))
    except Exception:
        store.release_inbound_claim(event_id)
        raise

    lead_record = {
        "event_id": event_id, "campaign_id": campaign_id, "platform": plat,
        "comment_id": comment_id.strip(), "commenter_id": actor_clean, "keyword": kw,
        "repo_url": campaign["repo_url"], "booking_url": campaign["booking_url"],
    }
    if on_lead_captured is not None:
        on_lead_captured(lead_record)

    return {
        "status": "dispatched", "event_id": event_id, "public_nudge": nudge_text,
        "dm_payload": dm_payload, "dm_bubble_count": dm_bubbles, "lead": lead_record,
    }

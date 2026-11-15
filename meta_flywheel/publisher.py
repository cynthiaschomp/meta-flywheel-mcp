# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Pre-publish public-200 gate, human approval enforcement, 3-channel publisher, and partial retry."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

from .approval import HumanApprovalRequiredError, build_stage2_approval_payload, consume_human_approval, stage_approval_request, verify_human_approval_ready
from .auth import GRAPH_FB_BASE, GRAPH_THREADS_V1, default_api_transport
from .db import FlywheelStore
from .urls import ProbeTransport, normalize_https_url, probe_public_url

ApiTransport = Callable[[str, str, Dict[str, Any]], Dict[str, Any]]
MediaHttpHook = Callable[[str, str], int]


class PublishGateError(RuntimeError):
    """Raised when strict_public audit or unauthenticated 200 OK link probes fail."""


def _default_media_hook(method: str, url: str) -> int:
    req = urllib.request.Request(normalize_https_url(url), method=method)
    with urllib.request.urlopen(req, timeout=8.0) as resp:
        return int(resp.getcode() or 0)


def _dry_run_api(method: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if method == "GET":
        return {"status_code": "FINISHED", "status": "FINISHED"}
    tail = endpoint.rstrip("/").split("/")[-1]
    return {"id": f"dry_{tail}_{abs(hash(json.dumps(params, sort_keys=True))) % 100000}"}


def verify_prepublish_gate(
    *, repo_url: str, booking_url: str, strict_public_passed: bool, probe_transport: Optional[ProbeTransport] = None
) -> Dict[str, Any]:
    """Block publishing unless strict_public audit passed and both public URLs return 200 OK."""
    if not strict_public_passed:
        raise PublishGateError("Pre-publish gate blocked: strict_public repository audit has not passed.")
    repo_probe = probe_public_url(repo_url, transport=probe_transport)
    if not repo_probe["ok"] or repo_probe["status_code"] != 200:
        raise PublishGateError(f"Public repo_url failed unauthenticated 200 OK check: {repo_probe}")
    book_probe = probe_public_url(booking_url, transport=probe_transport)
    if not book_probe["ok"] or book_probe["status_code"] != 200:
        raise PublishGateError(f"Public booking_url failed unauthenticated 200 OK check: {book_probe}")
    return {"repo_probe": repo_probe, "booking_probe": book_probe}


def stage_campaign_for_approval(
    *, store: FlywheelStore, campaign_id: str, adapted_copy: Dict[str, Any],
    channels: Optional[List[str]] = None, staged_media_url: Optional[str] = None,
    probe_transport: Optional[ProbeTransport] = None, verify_urls: bool = False,
) -> Dict[str, Any]:
    """Freeze adapted channel copy under a SHA-256 digest in PENDING state for human review."""
    campaign = store.get_campaign(campaign_id)
    if verify_urls or probe_transport is not None:
        verify_prepublish_gate(repo_url=campaign["repo_url"], booking_url=campaign["booking_url"], strict_public_passed=bool(campaign["strict_public_passed"]), probe_transport=probe_transport)
    elif not bool(campaign["strict_public_passed"]):
        raise PublishGateError("Pre-publish gate blocked: strict_public repository audit has not passed.")
    selected = channels if channels is not None else ["fb_page", "ig_pro", "threads"]
    payload = build_stage2_approval_payload(
        campaign_id=campaign_id, repo_url=campaign["repo_url"], booking_url=campaign["booking_url"],
        keyword=campaign["keyword"], channels=selected, adapted_copy=adapted_copy, staged_media_url=staged_media_url,
    )
    return stage_approval_request(store, stage="stage2_social", subject_id=campaign_id, payload=payload)


def _poll_container(api: ApiTransport, endpoint: str, field: str, token: str, max_polls: int, sleep_fn: Callable[[float], None]) -> None:
    for attempt in range(max_polls):
        state = str(api("GET", endpoint, {"fields": field, "access_token": token}).get(field) or "").upper()
        if state == "FINISHED":
            return
        if state in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Media container failed with {field}={state} on {endpoint}")
        if attempt + 1 < max_polls:
            sleep_fn(min(2.0, 0.25 * (2**attempt)))
    raise TimeoutError(f"Timed out polling {field}=='FINISHED' on {endpoint}")


def _publish_single_channel(
    *, store: FlywheelStore, job_id: str, channel: str, payload: Dict[str, Any], actors: Dict[str, str],
    tokens: Dict[str, str], api: ApiTransport, max_polls: int, sleep_fn: Callable[[float], None], existing_published_id: Optional[str] = None,
) -> Dict[str, Any]:
    store.transition_channel(job_id, channel, "STAGED", increment_attempt=True)
    try:
        if channel == "fb_page":
            page_id, token = actors["page_id"], tokens["page_token"]
            res = api("POST", f"{GRAPH_FB_BASE}/{page_id}/photos", {"url": payload["image_url"], "caption": payload["message"], "access_token": token}) if payload.get("image_url") else api("POST", f"{GRAPH_FB_BASE}/{page_id}/feed", {"message": payload["message"], "access_token": token})
            pub_id = str(res["id"])
            store.transition_channel(job_id, channel, "CONTAINER_CREATED", container_id=pub_id)
            return store.transition_channel(job_id, channel, "PUBLISHED", published_id=pub_id)
        if channel == "ig_pro":
            ig_id, token, img = actors["ig_user_id"], tokens["page_token"], payload.get("image") or {}
            if not img.get("url"):
                raise ValueError("Instagram publishing requires a staged HTTPS image URL.")
            cid = str(api("POST", f"{GRAPH_FB_BASE}/{ig_id}/media", {"image_url": img["url"], "caption": payload["caption"], "alt_text": img["alt_text"], "access_token": token})["id"])
            store.transition_channel(job_id, channel, "CONTAINER_CREATED", container_id=cid)
            _poll_container(api, f"{GRAPH_FB_BASE}/{cid}", "status_code", token, max_polls, sleep_fn)
            p_res = api("POST", f"{GRAPH_FB_BASE}/{ig_id}/media_publish", {"creation_id": cid, "access_token": token})
            return store.transition_channel(job_id, channel, "PUBLISHED", published_id=str(p_res["id"]))
        if channel == "threads":
            th_id, token = actors["threads_user_id"], tokens["threads_token"]
            if existing_published_id:
                p1_id = str(existing_published_id)
                store.transition_channel(job_id, channel, "CONTAINER_CREATED", published_id=p1_id)
            else:
                c1_id = str(api("POST", f"{GRAPH_THREADS_V1}/{th_id}/threads", {"media_type": "TEXT", "text": payload["post_1"], "topic_tag": payload["topic_tag"], "access_token": token})["id"])
                store.transition_channel(job_id, channel, "CONTAINER_CREATED", container_id=c1_id)
                _poll_container(api, f"{GRAPH_THREADS_V1}/{c1_id}", "status", token, max_polls, sleep_fn)
                p1_id = str(api("POST", f"{GRAPH_THREADS_V1}/{th_id}/threads_publish", {"creation_id": c1_id, "access_token": token})["id"])
                store.checkpoint_published_id(job_id, channel, p1_id)
            c2_id = str(api("POST", f"{GRAPH_THREADS_V1}/{th_id}/threads", {"media_type": "TEXT", "text": payload["post_2"], "reply_to_id": p1_id, "access_token": token})["id"])
            _poll_container(api, f"{GRAPH_THREADS_V1}/{c2_id}", "status", token, max_polls, sleep_fn)
            p2 = api("POST", f"{GRAPH_THREADS_V1}/{th_id}/threads_publish", {"creation_id": c2_id, "access_token": token})
            return store.transition_channel(job_id, channel, "PUBLISHED", published_id=p1_id, reply_published_id=str(p2["id"]))
        raise ValueError(f"Unsupported channel: {channel}")
    except Exception as exc:
        return store.transition_channel(job_id, channel, "FAILED", error=str(exc))


def publish_campaign(
    *, store: FlywheelStore, campaign_id: str, adapted_copy: Dict[str, Any], actors: Dict[str, str], tokens: Dict[str, str],
    channels: Optional[List[str]] = None, staged_media_url: Optional[str] = None, approval: Optional[Mapping[str, Any]] = None,
    api_transport: Optional[ApiTransport] = None, probe_transport: Optional[ProbeTransport] = None,
    media_hook: Optional[MediaHttpHook] = None, dry_run: bool = False, max_polls: int = 4, sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Verify human SHA-256 approval & 200 OK/media gates BEFORE consuming grant and publishing."""
    campaign = store.get_campaign(campaign_id)
    selected = channels if channels is not None else ["fb_page", "ig_pro", "threads"]
    stage2_payload = build_stage2_approval_payload(
        campaign_id=campaign_id, repo_url=campaign["repo_url"], booking_url=campaign["booking_url"],
        keyword=campaign["keyword"], channels=selected, adapted_copy=adapted_copy, staged_media_url=staged_media_url,
    )
    if not dry_run:
        verify_human_approval_ready(store, approval=approval, stage="stage2_social", current_payload=stage2_payload)
    if not dry_run or probe_transport is not None:
        verify_prepublish_gate(repo_url=campaign["repo_url"], booking_url=campaign["booking_url"], strict_public_passed=bool(campaign["strict_public_passed"]), probe_transport=probe_transport)
    elif not bool(campaign["strict_public_passed"]):
        raise PublishGateError("Pre-publish gate blocked: strict_public repository audit has not passed.")
    api, m_hook = api_transport or (_dry_run_api if dry_run else default_api_transport), media_hook or _default_media_hook
    norm_media, media_deleted = (normalize_https_url(staged_media_url) if staged_media_url else None), False
    if norm_media and (not dry_run or media_hook is not None) and m_hook("HEAD", norm_media) != 200:
        raise PublishGateError(f"Staged media HEAD verification failed for {norm_media}")
    consumed = consume_human_approval(store, approval=approval, stage="stage2_social", current_payload=stage2_payload) if not dry_run else None
    if not dry_run:
        store.mark_campaign_approved(campaign_id)
    batch = {ch: adapted_copy[ch] for ch in selected}
    job_id = store.create_publish_job(campaign_id, batch)
    try:
        results = [_publish_single_channel(store=store, job_id=job_id, channel=ch, payload=batch[ch], actors=actors, tokens=tokens, api=api, max_polls=max_polls, sleep_fn=sleep_fn) for ch in selected]
    finally:
        if norm_media and (not dry_run or media_hook is not None):
            media_deleted = m_hook("DELETE", norm_media) in {200, 202, 204}
    return {"job_id": job_id, "campaign_id": campaign_id, "approval_id": consumed["approval_id"] if consumed else None, "deliveries": results, "staged_media_deleted": media_deleted}


def retry_failed_channels(
    *, store: FlywheelStore, job_id: str, actors: Dict[str, str], tokens: Dict[str, str],
    api_transport: Optional[ApiTransport] = None, dry_run: bool = False, max_polls: int = 4, sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Retry ONLY FAILED channels for job_id, enforcing prior human approval when dry_run=False."""
    deliveries = store.get_job_deliveries(job_id)
    if not dry_run and not bool(store.get_campaign(deliveries[0]["campaign_id"]).get("human_approved")):
        raise HumanApprovalRequiredError("Live channel retry requires a campaign previously approved by a human operator.")
    api = api_transport or (_dry_run_api if dry_run else default_api_transport)
    skipped_published, retried = [], []
    for row in deliveries:
        ch = row["channel"]
        if row["state"] == "PUBLISHED":
            skipped_published.append(ch)
        elif row["state"] == "FAILED":
            retried.append(ch)
            _publish_single_channel(store=store, job_id=job_id, channel=ch, payload=json.loads(row["payload_json"]), actors=actors, tokens=tokens, api=api, max_polls=max_polls, sleep_fn=sleep_fn, existing_published_id=row.get("published_id") or None)
    if not retried:
        raise ValueError(f"No FAILED channels to retry in job_id={job_id} (published={skipped_published}).")
    return {"job_id": job_id, "skipped_published": skipped_published, "retried": retried, "deliveries": store.get_job_deliveries(job_id)}

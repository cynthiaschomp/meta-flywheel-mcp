# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Deterministic multi-channel copy adapter for Facebook Page, IG Pro, and Threads."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .urls import normalize_https_url

MAX_THREADS_BYTES = 500
IG_HOOK_MAX_CHARS = 125
IG_MIN_ASPECT = 0.8
IG_MAX_ASPECT = 1.91

_LINK_IN_BIO_RE = re.compile(r"[^.!?\n]*\blink\s+in\s+bio\b[^.!?\n]*[.!?]?\s*", re.I)
_DM_ME_RE = re.compile(r"\bdm\s+me\b", re.I)
_RAW_URL_RE = re.compile(r"https?://\S+", re.I)


def utf8_byte_len(text: str) -> int:
    """Return exact UTF-8 byte count (multi-byte emoji safe)."""
    return len(text.encode("utf-8"))


def fit_utf8_bytes(text: str, max_bytes: int = MAX_THREADS_BYTES) -> str:
    """Deterministically trim UTF-8 string to <= max_bytes without splitting codepoints."""
    clean = text.strip()
    if utf8_byte_len(clean) <= max_bytes:
        return clean
    ellipsis = "…"
    budget = max_bytes - utf8_byte_len(ellipsis)
    if budget <= 0:
        return ""
    trimmed = clean.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip()
    return f"{trimmed}{ellipsis}"


def sanitize_topic_tag(raw_tag: str) -> str:
    """Sanitize a single Threads topic tag (1-50 chars, strips '.' and '&')."""
    if not isinstance(raw_tag, str):
        raise ValueError("topic_tag must be a string.")
    cleaned = re.sub(r"\s+", " ", raw_tag.strip().lstrip("#").replace(".", "").replace("&", "")).strip()
    if not (1 <= len(cleaned) <= 50):
        raise ValueError(f"Threads topic_tag must be 1..50 chars after sanitization (got {cleaned!r}).")
    return cleaned


def strip_link_in_bio_slop(text: str) -> str:
    """Remove low-reach 'link in bio' sentences and collapse extra blank lines."""
    scrubbed = re.sub(r"[ \t]{2,}", " ", _LINK_IN_BIO_RE.sub(" ", text))
    return re.sub(r"\n{3,}", "\n\n", scrubbed).strip()


def validate_threads_post(text: str, *, allow_links: bool = False) -> None:
    """Validate Threads UTF-8 <=500 byte ceiling, DM-bait block, and link policy."""
    if not text.strip():
        raise ValueError("Threads post text cannot be empty.")
    if _DM_ME_RE.search(text):
        raise ValueError("Threads policy violation: 'DM me' phrasing is blocked.")
    if not allow_links and _RAW_URL_RE.search(text):
        raise ValueError("Threads post_1 must remain link-free so links live in chained post_2.")
    byte_count = utf8_byte_len(text)
    if byte_count > MAX_THREADS_BYTES:
        raise ValueError(f"Threads post exceeds {MAX_THREADS_BYTES} UTF-8 bytes ({byte_count} bytes).")


def _extract_hook(brief: str, explicit_hook: Optional[str]) -> str:
    if explicit_hook and explicit_hook.strip():
        return explicit_hook.strip()
    first_line = brief.strip().splitlines()[0].strip()
    return re.split(r"(?<=[.!?])\s+", first_line, maxsplit=1)[0].strip()


def adapt_campaign_copy(
    *,
    brief: str,
    repo_url: str,
    booking_url: str,
    keyword: str,
    hook: Optional[str] = None,
    topic_tag: str = "OpenSourceAI",
    image: Optional[Dict[str, Any]] = None,
    threads_post_1_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile master brief into FB Page, IG Professional, and Auto-Chained Threads payloads."""
    if not brief or not brief.strip():
        raise ValueError("Master brief must be non-empty.")
    kw = keyword.strip().upper()
    if not kw or not re.fullmatch(r"[A-Z0-9_-]{2,32}", kw):
        raise ValueError(f"Invalid trigger keyword {keyword!r}; use 2-32 alphanumeric chars.")
    if _DM_ME_RE.search(brief) or (hook and _DM_ME_RE.search(hook)):
        raise ValueError("Blocked 'DM me' phrasing in source brief/hook.")

    norm_repo, norm_booking = normalize_https_url(repo_url), normalize_https_url(booking_url)
    clean_brief = strip_link_in_bio_slop(brief)
    resolved_hook = strip_link_in_bio_slop(_extract_hook(clean_brief, hook))
    if not resolved_hook or len(resolved_hook) > IG_HOOK_MAX_CHARS:
        raise ValueError(f"Opening hook must be 1..{IG_HOOK_MAX_CHARS} chars for IG fold (got {len(resolved_hook)} chars).")

    cta_line = f'Comment "{kw}" below and I will send the open-source repo + walkthrough to your inbox.'
    fb_body = clean_brief if clean_brief.startswith(resolved_hook) else f"{resolved_hook}\n\n{clean_brief}"
    fb_text = f"{_RAW_URL_RE.sub('', fb_body).strip()}\n\n{cta_line}"

    ig_image_meta: Optional[Dict[str, Any]] = None
    if image is not None:
        alt_text = str(image.get("alt_text") or "").strip()
        if not alt_text:
            raise ValueError("Instagram image attachment requires non-empty alt_text.")
        width, height = float(image.get("width") or 0), float(image.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError("Instagram image attachment requires positive width and height.")
        ratio = width / height
        if not (IG_MIN_ASPECT <= ratio <= IG_MAX_ASPECT):
            raise ValueError(f"Instagram aspect ratio {ratio:.3f} outside [{IG_MIN_ASPECT}, {IG_MAX_ASPECT}].")
        img_url = normalize_https_url(str(image["url"])) if image.get("url") else None
        ig_image_meta = {"alt_text": alt_text, "width": int(width), "height": int(height), "aspect_ratio": round(ratio, 4), "url": img_url}

    ig_caption = f"{resolved_hook}\n\n{_RAW_URL_RE.sub('', clean_brief).strip()}\n\n{cta_line}"
    clean_tag = sanitize_topic_tag(topic_tag)
    if threads_post_1_override is not None:
        post_1 = threads_post_1_override.strip()
        validate_threads_post(post_1, allow_links=False)
    else:
        post_1 = fit_utf8_bytes(_RAW_URL_RE.sub("", fb_body).strip(), MAX_THREADS_BYTES)
        validate_threads_post(post_1, allow_links=False)

    post_2 = f"Full open-source repo & tool: {norm_repo}\nWant help wiring this into your stack? Book a call: {norm_booking}"
    validate_threads_post(post_2, allow_links=True)

    return {
        "keyword": kw,
        "repo_url": norm_repo,
        "booking_url": norm_booking,
        "fb_page": {"message": fb_text, "cta_keyword": kw, "image_url": ig_image_meta["url"] if ig_image_meta else None},
        "ig_pro": {"caption": ig_caption, "hook": resolved_hook, "hook_chars": len(resolved_hook), "cta_keyword": kw, "image": ig_image_meta},
        "threads": {
            "topic_tag": clean_tag,
            "post_1": post_1,
            "post_1_bytes": utf8_byte_len(post_1),
            "post_2": post_2,
            "post_2_bytes": utf8_byte_len(post_2),
        },
    }

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Canonical HTTPS URL normalizer and unauthenticated public 200 OK verifier."""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

_BLOCKED_SUFFIX_PAIRS = frozenset(
    {
        ("localhost", "run"),
        ("lhr", "life"),
        ("trycloudflare", "com"),
    }
)
_AUTH_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "x-github-token",
        "x-auth-token",
    }
)

ProbeTransport = Callable[[str, Dict[str, str], float], Tuple[int, Dict[str, str]]]


def _is_disallowed_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _is_ephemeral_relay(host: str) -> bool:
    labels = [part for part in host.split(".") if part]
    if any(lbl == "ngrok" or lbl.startswith("ngrok-") for lbl in labels):
        return True
    if len(labels) >= 2 and (labels[-2], labels[-1]) in _BLOCKED_SUFFIX_PAIRS:
        return True
    return False


def normalize_https_url(raw_url: str) -> str:
    """Normalize an owned public HTTPS URL and reject HTTP, local, IP, or relay hosts."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("URL must be a non-empty string.")
    parsed = urllib.parse.urlsplit(raw_url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Only https:// URLs are permitted (got {raw_url!r}).")
    if parsed.username or parsed.password:
        raise ValueError("Userinfo in URLs is forbidden.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid URL port in {raw_url!r}.") from exc

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(f"Missing hostname in {raw_url!r}.")
    if host in {"localhost", "local"} or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError(f"Local hostname is forbidden: {host!r}.")
    if _is_disallowed_ip(host):
        raise ValueError(f"Loopback or non-public IP host is forbidden: {host!r}.")
    if _is_ephemeral_relay(host):
        raise ValueError(f"Ephemeral third-party relay domain is forbidden: {host!r}.")
    if "." not in host and ":" not in host:
        raise ValueError(f"Public FQDN required: {host!r}.")

    netloc = f"[{host}]" if ":" in host else host
    if port and port != 443:
        netloc = f"{netloc}:{port}"

    raw_path = parsed.path or ""
    if raw_path in ("", "/"):
        norm_path = raw_path
    else:
        norm_path = raw_path.rstrip("/")
        if not norm_path.startswith("/"):
            norm_path = "/" + norm_path

    return urllib.parse.urlunsplit(("https", netloc, norm_path, parsed.query, ""))


def strip_auth_headers(headers: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], list[str]]:
    """Return a clean header copy with all authentication/cookie headers removed."""
    clean: Dict[str, str] = {
        "User-Agent": "meta-flywheel-mcp/0.1 (+unauthenticated-probe)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    stripped: list[str] = []
    for key, val in (headers or {}).items():
        if key.strip().lower() in _AUTH_HEADER_NAMES:
            stripped.append(key)
        else:
            clean[key] = str(val)
    return clean, stripped


def probe_public_url(
    url: str,
    *,
    timeout: float = 8.0,
    headers: Optional[Dict[str, str]] = None,
    transport: Optional[ProbeTransport] = None,
) -> Dict[str, Any]:
    """Verify an unauthenticated HTTP 200 OK response on a public URL."""
    normalized = normalize_https_url(url)
    req_headers, stripped = strip_auth_headers(headers)

    if transport is not None:
        status_code, _ = transport(normalized, req_headers, timeout)
        return {
            "url": normalized,
            "status_code": int(status_code),
            "ok": int(status_code) == 200,
            "authenticated": False,
            "stripped_auth_headers": stripped,
        }

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler())
    req = urllib.request.Request(normalized, headers=req_headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = int(resp.getcode() or 0)
            return {
                "url": normalized,
                "status_code": code,
                "ok": code == 200,
                "authenticated": False,
                "stripped_auth_headers": stripped,
            }
    except urllib.error.HTTPError as err:
        return {
            "url": normalized,
            "status_code": int(err.code),
            "ok": False,
            "authenticated": False,
            "stripped_auth_headers": stripped,
            "error": f"HTTP {err.code}",
        }
    except urllib.error.URLError as err:
        return {
            "url": normalized,
            "status_code": 0,
            "ok": False,
            "authenticated": False,
            "stripped_auth_headers": stripped,
            "error": str(err.reason),
        }

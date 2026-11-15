# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Meta Graph v22.0 & Threads v1.0 transports, token doctor, Page/IG resolver, and refresher."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional

from .db import FlywheelStore

GRAPH_FB_BASE = "https://graph.facebook.com/v22.0"
GRAPH_THREADS_BASE = "https://graph.threads.net"
GRAPH_THREADS_V1 = "https://graph.threads.net/v1.0"
THREADS_REFRESH_MIN_AGE_SEC = 7 * 86400
THREADS_TTL_SEC = 60 * 86400

JsonHttpClient = Callable[[str, str, Optional[Dict[str, str]]], Dict[str, Any]]


def default_api_transport(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Live stdlib urllib.request transport for Meta Graph v22.0 and Threads v1.0 APIs."""
    payload = {k: str(v) for k, v in (params or {}).items() if v is not None}
    verb = method.upper()
    full_url = f"{endpoint}?{urllib.parse.urlencode(payload)}" if payload and verb == "GET" else endpoint
    body = urllib.parse.urlencode(payload).encode("utf-8") if payload and verb != "GET" else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(full_url, data=body, headers=headers, method=verb)
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_live_reply_sender(
    tokens: Optional[Mapping[str, str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Create a live urllib.request sender for FB/IG JSON messages/replies and Threads replies."""
    src_env = env if env is not None else os.environ
    tok_map = dict(tokens or {})

    def _send(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if "threads.net" in endpoint:
            th_tok = (tok_map.get("threads_token") or src_env.get("THREADS_ACCESS_TOKEN") or "").strip()
            c_res = default_api_transport("POST", endpoint, {**payload, "access_token": th_tok})
            if endpoint.rstrip("/").endswith("/threads") and c_res.get("id"):
                pub_ep = f"{endpoint.rstrip('/')}_publish"
                return default_api_transport("POST", pub_ep, {"creation_id": c_res["id"], "access_token": th_tok})
            return c_res

        pg_tok = (tok_map.get("page_token") or src_env.get("META_PAGE_ACCESS_TOKEN") or "").strip()
        raw = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if pg_tok:
            headers["Authorization"] = f"Bearer {pg_tok}"
        req = urllib.request.Request(endpoint, data=raw, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return _send


def _mask_secret(val: str) -> str:
    if not val:
        return "<unset>"
    return "****" if len(val) <= 8 else f"{val[:4]}...{val[-4:]}"


def _epoch_to_iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).replace(microsecond=0).isoformat()


class MetaAuthDoctor:
    """Manages Meta App LIVE readiness, User->Page token swap, IG ID resolution, and Threads refresh."""

    def __init__(self, env: Optional[Mapping[str, str]] = None, http_client: Optional[JsonHttpClient] = None) -> None:
        self.env: Mapping[str, str] = env if env is not None else os.environ
        self.http_client: JsonHttpClient = http_client or default_api_transport

    def _require_app_vars(self) -> tuple[str, str]:
        app_id = self.env.get("META_APP_ID", "").strip()
        app_secret = self.env.get("META_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise EnvironmentError("Missing required ${META_APP_ID} and/or ${META_APP_SECRET} environment variables.")
        return app_id, app_secret

    def exchange_long_lived_user_token(self, short_user_token: str) -> Dict[str, Any]:
        """Exchange a short-lived Meta User token for a 60-day token via fb_exchange_token."""
        if not short_user_token.strip():
            raise ValueError("short_user_token cannot be empty.")
        app_id, app_secret = self._require_app_vars()
        res = self.http_client(
            "GET",
            f"{GRAPH_FB_BASE}/oauth/access_token",
            {"grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret, "fb_exchange_token": short_user_token.strip()},
        )
        return {"access_token": res["access_token"], "token_type": res.get("token_type", "bearer"), "expires_in": int(res.get("expires_in", THREADS_TTL_SEC))}

    def swap_user_for_page_token(self, user_token: str, page_id: str) -> Dict[str, Any]:
        """Swap a User token into a Page token and resolve linked Instagram Professional user ID."""
        if not user_token.strip() or not page_id.strip():
            raise ValueError("user_token and page_id are required.")
        res = self.http_client(
            "GET",
            f"{GRAPH_FB_BASE}/{page_id.strip()}",
            {"fields": "id,name,access_token,instagram_business_account", "access_token": user_token.strip()},
        )
        ig_acct = res.get("instagram_business_account") or {}
        return {
            "page_id": str(res.get("id") or page_id.strip()),
            "page_name": str(res.get("name") or ""),
            "page_access_token": str(res["access_token"]),
            "ig_user_id": str(ig_acct.get("id") or ""),
        }

    def check_app_live_mode(self) -> Dict[str, Any]:
        """Inspect Meta App readiness and confirm LIVE mode."""
        app_id, app_secret = self._require_app_vars()
        res = self.http_client("GET", f"{GRAPH_FB_BASE}/{app_id}", {"fields": "id,name,link,category", "access_token": f"{app_id}|{app_secret}"})
        mode = (self.env.get("META_APP_MODE") or res.get("mode") or ("LIVE" if res.get("category") else "DEV")).upper()
        return {"app_id": app_id, "app_name": res.get("name", ""), "mode": mode, "live_ready": mode == "LIVE"}

    def refresh_threads_token(self, threads_token: str, issued_at_epoch: float, *, now_epoch: Optional[float] = None) -> Dict[str, Any]:
        """Refresh a 60-day Threads token via th_refresh_token once age >= 7 days."""
        if not threads_token.strip():
            raise ValueError("threads_token cannot be empty.")
        now = float(now_epoch if now_epoch is not None else time.time())
        age_sec = max(0.0, now - float(issued_at_epoch))
        if age_sec < THREADS_REFRESH_MIN_AGE_SEC:
            return {"refreshed": False, "reason": "age_under_7_days", "age_days": round(age_sec / 86400.0, 2), "expires_at": _epoch_to_iso(float(issued_at_epoch) + THREADS_TTL_SEC)}
        res = self.http_client("GET", f"{GRAPH_THREADS_BASE}/refresh_access_token", {"grant_type": "th_refresh_token", "access_token": threads_token.strip()})
        ttl = int(res.get("expires_in", THREADS_TTL_SEC))
        return {"refreshed": True, "access_token": res["access_token"], "expires_in": ttl, "issued_at": _epoch_to_iso(now), "expires_at": _epoch_to_iso(now + ttl)}

    def run_doctor(self, store: Optional[FlywheelStore] = None, *, dry_run: bool = True) -> Dict[str, Any]:
        """Run safe token & App readiness diagnostics (masks secrets in output)."""
        app_id = self.env.get("META_APP_ID", "").strip()
        app_secret = self.env.get("META_APP_SECRET", "").strip()
        app_mode = (self.env.get("META_APP_MODE") or "DEV").strip().upper()
        issued_epoch = float(self.env.get("THREADS_TOKEN_ISSUED_EPOCH") or time.time())
        age_days = round(max(0.0, time.time() - issued_epoch) / 86400.0, 2)
        status = "READY" if (app_id and app_secret and app_mode == "LIVE") else "NEEDS_CONFIG"
        if store is not None:
            store.upsert_token_health(
                subject_key=f"app:{app_id or 'unset'}", platform="meta", subject_id=app_id or "unset",
                app_mode=app_mode, issued_at=_epoch_to_iso(issued_epoch), expires_at=_epoch_to_iso(issued_epoch + THREADS_TTL_SEC),
                last_refreshed_at=None, status=status,
            )
        return {
            "dry_run": dry_run, "status": status, "app_id": app_id or "<unset>", "app_secret_masked": _mask_secret(app_secret),
            "app_mode": app_mode, "live_ready": app_mode == "LIVE",
            "page_id": self.env.get("META_PAGE_ID", "").strip() or "<unset>",
            "ig_user_id": self.env.get("IG_USER_ID", "").strip() or "<unset>",
            "threads_user_id": self.env.get("THREADS_USER_ID", "").strip() or "<unset>",
            "threads_token_age_days": age_days, "threads_refresh_due": age_days >= 7.0,
        }

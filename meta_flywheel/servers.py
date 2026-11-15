# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Stdlib HTTP servers for Meta `/events` webhook and `/stage` ephemeral media hosting."""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .db import FlywheelStore
from .webhook import LeadCapturedHook, ReplySender, process_comment_webhook, verify_hub_signature

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_OBJ_PLATFORM = {"page": "fb_page", "instagram": "ig_pro", "threads": "threads", "threads_business": "threads"}


def _send_bytes(h: BaseHTTPRequestHandler, code: int, raw: bytes, ctype: str = "application/json; charset=utf-8", send_body: bool = True) -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(raw)))
    h.end_headers()
    if send_body and raw:
        h.wfile.write(raw)


def _extract_webhook_events(payload: Dict[str, Any], fallback_cid: Optional[str]) -> List[Dict[str, str]]:
    if "comment_id" in payload and "comment_text" in payload:
        return [{"campaign_id": str(payload.get("campaign_id") or fallback_cid or ""), "platform": str(payload.get("platform") or "ig_pro"), "comment_id": str(payload["comment_id"]), "author_id": str(payload.get("author_id") or ""), "comment_text": str(payload["comment_text"])}]
    plat = _OBJ_PLATFORM.get(str(payload.get("object") or "").lower(), "ig_pro")
    events: List[Dict[str, str]] = []
    for entry in payload.get("entry") or []:
        for chg in entry.get("changes") or []:
            val = chg.get("value") or {}
            cid, txt = str(val.get("comment_id") or val.get("id") or "").strip(), str(val.get("text") or val.get("message") or "").strip()
            aid = str((val.get("from") or {}).get("id") or val.get("sender_id") or val.get("username") or "").strip()
            if cid and txt:
                events.append({"campaign_id": str(val.get("campaign_id") or fallback_cid or ""), "platform": plat, "comment_id": cid, "author_id": aid, "comment_text": txt})
    return events


def create_webhook_server(
    host: str = "127.0.0.1", port: int = 8620, *, store: Optional[FlywheelStore] = None, campaign_id: Optional[str] = None,
    verify_token: Optional[str] = None, app_secret: Optional[str] = None, actor_ids: Optional[Mapping[str, str]] = None,
    tokens: Optional[Mapping[str, str]] = None, reply_sender: Optional[ReplySender] = None,
    on_lead_captured: Optional[LeadCapturedHook] = None, dry_run: bool = False,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer serving GET/POST `/events` for Meta webhooks."""
    db = store or FlywheelStore(":memory:")

    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path.rstrip("/") != "/events":
                return _send_bytes(self, 404, b'{"error":"not_found"}')
            qs = urllib.parse.parse_qs(parsed.query)
            mode, tok, chal = (qs.get("hub.mode") or [""])[0], (qs.get("hub.verify_token") or [""])[0], (qs.get("hub.challenge") or [""])[0]
            expected = (verify_token if verify_token is not None else os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")).strip()
            if mode == "subscribe" and expected and hmac.compare_digest(tok, expected):
                return _send_bytes(self, 200, chal.encode("utf-8"), "text/plain; charset=utf-8")
            _send_bytes(self, 403, b'{"error":"verification_failed"}')

        def do_POST(self) -> None:
            if urllib.parse.urlsplit(self.path).path.rstrip("/") != "/events":
                return _send_bytes(self, 404, b'{"error":"not_found"}')
            raw_body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            sig = self.headers.get("X-Hub-Signature-256") or ""
            secret = (app_secret if app_secret is not None else os.environ.get("META_APP_SECRET", "")).strip()
            if not verify_hub_signature(raw_body, sig, secret):
                return _send_bytes(self, 401, b'{"status":"rejected","reason":"invalid_signature"}')
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            default_cid = campaign_id or db.latest_campaign_id()
            results = [
                process_comment_webhook(
                    store=db, campaign_id=ev["campaign_id"], platform=ev["platform"], comment_id=ev["comment_id"],
                    author_id=ev["author_id"], comment_text=ev["comment_text"], actor_ids=actor_ids, tokens=tokens,
                    reply_sender=reply_sender, on_lead_captured=on_lead_captured, dry_run=dry_run,
                )
                for ev in _extract_webhook_events(payload, default_cid)
                if ev["campaign_id"]
            ]
            _send_bytes(self, 200, json.dumps({"status": "ok", "results": results}).encode("utf-8"))

    return ThreadingHTTPServer((host, port), WebhookHandler)


def serve_webhook(host: str = "127.0.0.1", port: int = 8620, **kwargs: Any) -> None:
    """Run blocking `/events` webhook HTTP server."""
    srv = create_webhook_server(host=host, port=port, **kwargs)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def purge_expired_media(stage_dir: Path, ttl_seconds: int = 86400, now_ts: Optional[float] = None) -> int:
    """Delete staged files older than ttl_seconds (default 24h) and return count purged."""
    if not stage_dir.is_dir():
        return 0
    now, removed = float(now_ts if now_ts is not None else time.time()), 0
    for fp in stage_dir.iterdir():
        if fp.is_file() and (now - fp.stat().st_mtime) > ttl_seconds:
            fp.unlink(missing_ok=True)
            removed += 1
    return removed


def create_media_stage_server(
    host: str = "127.0.0.1", port: int = 8621, stage_dir: Optional[str | Path] = None, ttl_seconds: int = 86400
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer serving PUT/GET/HEAD/DELETE `/stage/<name>` with 24h TTL."""
    root = Path(stage_dir) if stage_dir else Path(tempfile.mkdtemp(prefix="flywheel_stage_"))
    root.mkdir(parents=True, exist_ok=True)

    class MediaStageHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _target(self) -> Optional[Path]:
            purge_expired_media(root, ttl_seconds=ttl_seconds)
            path = urllib.parse.urlsplit(self.path).path
            name = path[7:] if path.startswith("/stage/") else ""
            return (root / name) if _SAFE_NAME_RE.fullmatch(name) and ".." not in name else None

        def do_PUT(self) -> None:
            fp = self._target()
            if fp is None:
                return _send_bytes(self, 400, b'{"error":"invalid_stage_path"}')
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            fp.write_bytes(data)
            _send_bytes(self, 201, json.dumps({"staged": fp.name, "bytes": len(data)}).encode("utf-8"))

        def do_HEAD(self) -> None:
            fp = self._target()
            if fp is None or not fp.is_file():
                return _send_bytes(self, 404, b"", send_body=False)
            ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
            _send_bytes(self, 200, fp.read_bytes(), ctype, send_body=False)

        def do_GET(self) -> None:
            fp = self._target()
            if fp is None or not fp.is_file():
                return _send_bytes(self, 404, b'{"error":"not_found"}')
            ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
            _send_bytes(self, 200, fp.read_bytes(), ctype)

        def do_DELETE(self) -> None:
            fp = self._target()
            if fp is None or not fp.is_file():
                return _send_bytes(self, 404, b'{"error":"not_found"}')
            fp.unlink(missing_ok=True)
            _send_bytes(self, 200, json.dumps({"deleted": fp.name}).encode("utf-8"))

    return ThreadingHTTPServer((host, port), MediaStageHandler)


def serve_media_stage(host: str = "127.0.0.1", port: int = 8621, stage_dir: Optional[str | Path] = None, ttl_seconds: int = 86400) -> None:
    """Run blocking `/stage/<name>` media staging HTTP server."""
    srv = create_media_stage_server(host=host, port=port, stage_dir=stage_dir, ttl_seconds=ttl_seconds)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()

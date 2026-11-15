# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""SQLite campaign, channel state machine, comment idempotency, and token store."""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .urls import normalize_https_url

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
VALID_PREFIXES = frozenset({"cmp", "job", "evt"})
ALLOWED_TRANSITIONS = {
    "PENDING": {"STAGED", "FAILED"},
    "STAGED": {"CONTAINER_CREATED", "PUBLISHED", "FAILED"},
    "CONTAINER_CREATED": {"PUBLISHED", "FAILED"},
    "FAILED": {"STAGED"},
    "PUBLISHED": set(),
}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def make_id(prefix: str) -> str:
    """Generate a collision-proof timestamp ID with 4-hex-char random entropy."""
    if prefix not in VALID_PREFIXES:
        raise ValueError(f"Unsupported ID prefix {prefix!r}; expected {sorted(VALID_PREFIXES)}.")
    return f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(2)}"


class FlywheelStore:
    """SQLite persistence spine for campaigns, channel deliveries, and webhook idempotency."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def create_campaign(
        self, *, title: str, brief: str, keyword: str, repo_url: str, booking_url: str, strict_public_passed: bool = False
    ) -> Dict[str, Any]:
        if not title.strip() or not brief.strip() or not keyword.strip():
            raise ValueError("Campaign title, brief, and keyword must all be non-empty.")
        cid = make_id("cmp")
        norm_repo, norm_book = normalize_https_url(repo_url), normalize_https_url(booking_url)
        self._conn.execute(
            "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, title.strip(), brief.strip(), keyword.strip().upper(), norm_repo, norm_book, int(bool(strict_public_passed)), utc_now_iso()),
        )
        self._conn.commit()
        return self.get_campaign(cid)

    def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        row = self._conn.execute("SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown campaign_id: {campaign_id}")
        return dict(row)

    def latest_campaign_id(self) -> Optional[str]:
        row = self._conn.execute("SELECT campaign_id FROM campaigns ORDER BY rowid DESC LIMIT 1").fetchone()
        return str(row["campaign_id"]) if row else None

    def create_publish_job(self, campaign_id: str, channel_payloads: Dict[str, Dict[str, Any]]) -> str:
        if not channel_payloads:
            raise ValueError("Empty channel batch rejected: at least one channel delivery is required.")
        self.get_campaign(campaign_id)
        job_id, now = make_id("job"), utc_now_iso()
        for channel, payload in channel_payloads.items():
            self._conn.execute(
                """INSERT INTO channel_deliveries
                   (delivery_id, job_id, campaign_id, channel, state, payload_json, attempts, updated_at)
                   VALUES (?, ?, ?, ?, 'PENDING', ?, 0, ?)""",
                (make_id("job"), job_id, campaign_id, channel, json.dumps(payload, sort_keys=True), now),
            )
        self._conn.commit()
        return job_id

    def checkpoint_published_id(self, job_id: str, channel: str, published_id: str) -> None:
        """Persist primary post ID before chained follow-up steps so retries never double-post."""
        self._conn.execute(
            "UPDATE channel_deliveries SET published_id = ?, updated_at = ? WHERE job_id = ? AND channel = ?",
            (published_id, utc_now_iso(), job_id, channel),
        )
        self._conn.commit()

    def transition_channel(
        self,
        job_id: str,
        channel: str,
        new_state: str,
        *,
        container_id: Optional[str] = None,
        published_id: Optional[str] = None,
        reply_published_id: Optional[str] = None,
        error: Optional[str] = None,
        increment_attempt: bool = False,
    ) -> Dict[str, Any]:
        row = self._conn.execute("SELECT * FROM channel_deliveries WHERE job_id = ? AND channel = ?", (job_id, channel)).fetchone()
        if not row:
            raise KeyError(f"Delivery not found for job={job_id}, channel={channel}")
        if new_state not in ALLOWED_TRANSITIONS.get(row["state"], set()):
            raise ValueError(f"Invalid state transition {row['state']} -> {new_state} on {channel}")
        attempts = int(row["attempts"]) + (1 if increment_attempt else 0)
        self._conn.execute(
            """UPDATE channel_deliveries SET state = ?, container_id = COALESCE(?, container_id),
               published_id = COALESCE(?, published_id), reply_published_id = COALESCE(?, reply_published_id),
               attempts = ?, last_error = ?, updated_at = ? WHERE job_id = ? AND channel = ?""",
            (new_state, container_id, published_id, reply_published_id, attempts, error, utc_now_iso(), job_id, channel),
        )
        self._conn.commit()
        return dict(self._conn.execute("SELECT * FROM channel_deliveries WHERE job_id = ? AND channel = ?", (job_id, channel)).fetchone())

    def get_job_deliveries(self, job_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM channel_deliveries WHERE job_id = ? ORDER BY rowid ASC", (job_id,)).fetchall()
        if not rows:
            raise KeyError(f"No deliveries found for job_id={job_id}")
        return [dict(r) for r in rows]

    def claim_inbound_comment(
        self, *, campaign_id: str, platform: str, comment_id: str, commenter_id: str, comment_text: str, matched_keyword: str
    ) -> Tuple[bool, str]:
        if not comment_id.strip() or not commenter_id.strip():
            raise ValueError("comment_id and commenter_id must be non-empty.")
        if self._conn.execute("SELECT 1 FROM inbound_comments WHERE platform = ? AND comment_id = ?", (platform, comment_id)).fetchone():
            return False, "duplicate_comment_id"
        if self._conn.execute("SELECT 1 FROM inbound_comments WHERE campaign_id = ? AND platform = ? AND commenter_id = ?", (campaign_id, platform, commenter_id)).fetchone():
            return False, "duplicate_campaign_commenter"
        evt_id = make_id("evt")
        try:
            self._conn.execute(
                "INSERT INTO inbound_comments (event_id, campaign_id, platform, comment_id, commenter_id, comment_text, matched_keyword, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (evt_id, campaign_id, platform, comment_id, commenter_id, comment_text, matched_keyword, utc_now_iso()),
            )
            self._conn.commit()
            return True, evt_id
        except sqlite3.IntegrityError:
            return False, "duplicate_constraint"

    def release_inbound_claim(self, event_id: str) -> None:
        """Roll back an inbound_comments claim when outbound reply delivery fails."""
        self._conn.execute("DELETE FROM inbound_comments WHERE event_id = ?", (event_id,))
        self._conn.commit()

    def record_comment_dispatch(self, event_id: str, public_reply_id: str, dm_message_id: str) -> None:
        self._conn.execute("UPDATE inbound_comments SET public_reply_id = ?, dm_message_id = ? WHERE event_id = ?", (public_reply_id, dm_message_id, event_id))
        self._conn.commit()

    def upsert_token_health(self, *, subject_key: str, platform: str, subject_id: str, app_mode: str, issued_at: str, expires_at: Optional[str], last_refreshed_at: Optional[str], status: str) -> None:
        self._conn.execute(
            """INSERT INTO token_health VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(subject_key) DO UPDATE SET platform = excluded.platform, subject_id = excluded.subject_id,
               app_mode = excluded.app_mode, issued_at = excluded.issued_at, expires_at = excluded.expires_at,
               last_refreshed_at = excluded.last_refreshed_at, status = excluded.status, checked_at = excluded.checked_at""",
            (subject_key, platform, subject_id, app_mode, issued_at, expires_at, last_refreshed_at, status, utc_now_iso()),
        )
        self._conn.commit()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""Human-in-the-loop SHA-256 approval gate for Stage 1 repo and Stage 2 social publishing."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .db import FlywheelStore, make_id, utc_now_iso
from .urls import normalize_https_url

VALID_STAGES = frozenset({"stage1_repo", "stage2_social"})
VALID_VIA = frozenset({"interactive_tty", "dashboard_ui"})


class HumanApprovalRequiredError(RuntimeError):
    """Raised when live publishing is attempted without a valid, matching human approval grant."""


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_stage2_approval_payload(
    *, campaign_id: str, repo_url: str, booking_url: str, keyword: str,
    channels: Sequence[str], adapted_copy: Mapping[str, Any], staged_media_url: Optional[str] = None,
) -> Dict[str, Any]:
    selected = [str(ch).strip() for ch in channels if str(ch).strip()]
    if not selected:
        raise ValueError("Empty channel list rejected: at least one channel is required.")
    return {
        "campaign_id": campaign_id.strip(), "repo_url": normalize_https_url(repo_url),
        "booking_url": normalize_https_url(booking_url), "keyword": keyword.strip().upper(),
        "channels": selected, "channel_copy": {ch: adapted_copy[ch] for ch in selected},
        "staged_media_url": normalize_https_url(staged_media_url) if staged_media_url else None,
    }


def _row_to_record(row: Mapping[str, Any]) -> Dict[str, Any]:
    rec = dict(row)
    rec.pop("approval_token_hash", None)
    rec["payload"] = json.loads(rec.pop("payload_json"))
    rec["digest_short"] = rec["payload_sha256"][:8].upper()
    return rec


def stage_approval_request(store: FlywheelStore, *, stage: str, subject_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    if stage not in VALID_STAGES or not subject_id.strip():
        raise ValueError(f"Invalid stage={stage!r} or empty subject_id.")
    aid = make_id("apr")
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    store._conn.execute(
        "INSERT INTO approval_requests (approval_id, stage, subject_id, payload_json, payload_sha256, status, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
        (aid, stage, subject_id.strip(), canonical_json, digest, utc_now_iso()),
    )
    store._conn.commit()
    return get_approval_request(store, aid)


def get_approval_request(store: FlywheelStore, approval_id: str) -> Dict[str, Any]:
    row = store._conn.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)).fetchone()
    if not row:
        raise KeyError(f"Unknown approval_id: {approval_id}")
    return _row_to_record(row)


def list_approval_requests(store: FlywheelStore, *, status: Optional[str] = None, stage: Optional[str] = None) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status.strip().upper())
    if stage:
        clauses.append("stage = ?"); params.append(stage.strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [_row_to_record(r) for r in store._conn.execute(f"SELECT * FROM approval_requests {where} ORDER BY rowid DESC", params).fetchall()]


def grant_human_approval(
    store: FlywheelStore, *, approval_id: str, expected_sha256: str,
    approved_by: str = "human_operator", approved_via: str = "dashboard_ui",
) -> Dict[str, Any]:
    if approved_via not in VALID_VIA or not approved_by.strip():
        raise ValueError(f"Invalid approved_via={approved_via!r} or empty approved_by.")
    row = store._conn.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)).fetchone()
    if not row:
        raise KeyError(f"Unknown approval_id: {approval_id}")
    if row["status"] != "PENDING":
        raise HumanApprovalRequiredError(f"Approval {approval_id} is {row['status']}, expected PENDING.")
    if not hmac.compare_digest(row["payload_sha256"], expected_sha256.strip().lower()):
        raise HumanApprovalRequiredError("Payload SHA-256 mismatch: draft changed after staging.")
    raw_token, now = secrets.token_urlsafe(24), utc_now_iso()
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    cur = store._conn.execute(
        "UPDATE approval_requests SET status = 'APPROVED', approval_token_hash = ?, approved_by = ?, approved_via = ?, decided_at = ? WHERE approval_id = ? AND status = 'PENDING'",
        (token_hash, approved_by.strip(), approved_via, now, approval_id),
    )
    store._conn.commit()
    if cur.rowcount != 1:
        raise HumanApprovalRequiredError(f"Concurrent state change prevented approving {approval_id}.")
    return {"approval_id": approval_id, "approval_token": raw_token, "payload_sha256": row["payload_sha256"], "approved_by": approved_by.strip(), "approved_via": approved_via, "decided_at": now}


def reject_approval_request(store: FlywheelStore, *, approval_id: str, rejected_by: str = "human_operator") -> Dict[str, Any]:
    cur = store._conn.execute(
        "UPDATE approval_requests SET status = 'REJECTED', approved_by = ?, decided_at = ? WHERE approval_id = ? AND status = 'PENDING'",
        (rejected_by.strip() or "human_operator", utc_now_iso(), approval_id),
    )
    store._conn.commit()
    if cur.rowcount != 1:
        raise HumanApprovalRequiredError(f"Approval {approval_id} is no longer PENDING.")
    return get_approval_request(store, approval_id)


def format_approval_summary(record: Mapping[str, Any]) -> str:
    pl = record["payload"]
    lines = [
        f"=== HUMAN PUBLISHING APPROVAL GATE ({record['stage']}) ===",
        f"Approval ID : {record['approval_id']} | Subject: {record['subject_id']}",
        f"SHA-256     : {record['payload_sha256']} (Digest {record['digest_short']})",
    ]
    if record["stage"] == "stage1_repo":
        lines += [f"GitHub Repo : {pl.get('repo')} ({pl.get('repo_url')})", f"Tree SHA256 : {pl.get('tree_sha256')}", f"About       : {pl.get('about')}"]
    else:
        lines.append(f"Links       : {pl.get('repo_url')} | {pl.get('booking_url')} | Keyword={pl.get('keyword')}")
        lines += [f"--- [{ch}] ---\n{json.dumps(body, indent=2, ensure_ascii=False)}" for ch, body in (pl.get("channel_copy") or {}).items()]
    return "\n".join(lines)


def prompt_tty_human_approval(
    store: FlywheelStore, *, approval_id: str, approved_by: str = "cli_operator", tty_io: Optional[Callable[[str], str]] = None
) -> Dict[str, Any]:
    rec = get_approval_request(store, approval_id)
    banner = format_approval_summary(rec) + "\nType APPROVE to authorize live publishing (any other input rejects): "
    if tty_io is not None:
        answer = tty_io(banner)
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise HumanApprovalRequiredError("Interactive human terminal (/dev/tty) required; non-TTY/agent execution cannot approve live publishing.")
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write(banner); tty.flush(); answer = tty.readline()
        except OSError as exc:
            raise HumanApprovalRequiredError("Unable to open /dev/tty for interactive human approval.") from exc
    if answer.strip() != "APPROVE":
        reject_approval_request(store, approval_id=approval_id, rejected_by=approved_by)
        raise HumanApprovalRequiredError(f"Live publishing rejected by human operator for {approval_id}.")
    return grant_human_approval(store, approval_id=approval_id, expected_sha256=rec["payload_sha256"], approved_by=approved_by, approved_via="interactive_tty")


def verify_human_approval_ready(store: FlywheelStore, *, approval: Optional[Mapping[str, Any]], stage: str, current_payload: Mapping[str, Any]) -> str:
    if not approval or not str(approval.get("approval_id") or "").strip() or not str(approval.get("approval_token") or "").strip():
        raise HumanApprovalRequiredError(f"Human final approval is required before live {stage} publishing.")
    aid, tok = str(approval["approval_id"]).strip(), str(approval["approval_token"]).strip()
    row = store._conn.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (aid,)).fetchone()
    if not row or row["stage"] != stage or row["status"] != "APPROVED":
        raise HumanApprovalRequiredError(f"Approval {aid} invalid for {stage} (status={row['status'] if row else 'missing'}).")
    if not hmac.compare_digest(row["approval_token_hash"] or "", hashlib.sha256(tok.encode("utf-8")).hexdigest()):
        raise HumanApprovalRequiredError("Invalid human approval token.")
    if not hmac.compare_digest(row["payload_sha256"], canonical_payload_sha256(current_payload)):
        raise HumanApprovalRequiredError("Payload SHA-256 changed after human approval; re-stage and re-approve.")
    return aid


def consume_human_approval(store: FlywheelStore, *, approval: Optional[Mapping[str, Any]], stage: str, current_payload: Mapping[str, Any]) -> Dict[str, Any]:
    aid = verify_human_approval_ready(store, approval=approval, stage=stage, current_payload=current_payload)
    cur = store._conn.execute("UPDATE approval_requests SET status = 'CONSUMED', consumed_at = ? WHERE approval_id = ? AND status = 'APPROVED'", (utc_now_iso(), aid))
    store._conn.commit()
    if cur.rowcount != 1:
        raise HumanApprovalRequiredError(f"Approval {aid} was already consumed.")
    return get_approval_request(store, aid)

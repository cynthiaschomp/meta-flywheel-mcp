# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""CLI and stdio JSON-RPC 2024-11-05 MCP server with mandatory human publishing approval."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .adapter import adapt_campaign_copy
from .approval import HumanApprovalRequiredError, get_approval_request, list_approval_requests, prompt_tty_human_approval
from .auth import MetaAuthDoctor
from .db import FlywheelStore
from .publisher import publish_campaign, retry_failed_channels, stage_campaign_for_approval
from .servers import serve_media_stage, serve_webhook
from .webhook import process_comment_webhook


def _tool(name: str, desc: str, props: Dict[str, Any], req: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if req:
        schema["required"] = req
    return {"name": name, "description": desc, "inputSchema": schema}


TOOL_SPECS: List[Dict[str, Any]] = [
    _tool("flywheel_adapt_copy", "Compile a master brief into FB Page, IG Pro, and Auto-Chained Threads payloads.", {"brief": {"type": "string"}, "repo_url": {"type": "string"}, "booking_url": {"type": "string"}, "keyword": {"type": "string"}, "hook": {"type": "string"}, "topic_tag": {"type": "string"}, "image": {"type": "object"}}, ["brief", "repo_url", "booking_url", "keyword"]),
    _tool("flywheel_stage_campaign", "Stage a SHA-256-locked campaign draft in PENDING human-approval state (agents cannot publish live).", {"title": {"type": "string"}, "brief": {"type": "string"}, "repo_url": {"type": "string"}, "booking_url": {"type": "string"}, "keyword": {"type": "string"}, "strict_public_passed": {"type": "boolean"}, "channels": {"type": "array", "items": {"type": "string"}}, "image": {"type": "object"}, "staged_media_url": {"type": "string"}}, ["title", "brief", "repo_url", "booking_url", "keyword"]),
    _tool("flywheel_list_approvals", "Inspect staged human approval requests (PENDING, APPROVED, REJECTED, CONSUMED).", {"status": {"type": "string"}, "stage": {"type": "string"}}),
    _tool("flywheel_publish_campaign", "Dry-run simulation only in MCP; live publishing (dry_run=false) is hard-blocked without human CLI/UI approval.", {"title": {"type": "string"}, "brief": {"type": "string"}, "repo_url": {"type": "string"}, "booking_url": {"type": "string"}, "keyword": {"type": "string"}, "strict_public_passed": {"type": "boolean"}, "channels": {"type": "array", "items": {"type": "string"}}, "dry_run": {"type": "boolean"}}, ["title", "brief", "repo_url", "booking_url", "keyword"]),
    _tool("flywheel_retry_failed", "Dry-run retry simulation only in MCP; live retries require CLI (--retry) or UI.", {"job_id": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["job_id"]),
    _tool("flywheel_simulate_webhook", "Dry-run simulation of inbound Meta comment webhook routing (live outbound replies are hard-blocked in MCP).", {"campaign_id": {"type": "string"}, "platform": {"type": "string"}, "comment_id": {"type": "string"}, "author_id": {"type": "string"}, "comment_text": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["campaign_id", "platform", "comment_id", "author_id", "comment_text"]),
    _tool("flywheel_token_doctor", "Inspect Meta App LIVE readiness, Page/IG bindings, and 60-day Threads token age.", {"dry_run": {"type": "boolean"}}),
]


class FlywheelMcpServer:
    """Stateful MCP tool dispatcher enforcing hard MCP live-mutation blocks across publish, retry, and webhooks."""

    def __init__(self, db_path: Optional[str] = None, default_dry_run: bool = False) -> None:
        self.store = FlywheelStore(db_path or os.environ.get("META_FLYWHEEL_DB") or ":memory:")
        self.default_dry_run = default_dry_run
        self.doctor = MetaAuthDoctor()

    def _actors_and_tokens(self) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        actors = {"page_id": os.environ.get("META_PAGE_ID", "page_dry"), "ig_user_id": os.environ.get("IG_USER_ID", "ig_dry"), "threads_user_id": os.environ.get("THREADS_USER_ID", "threads_dry")}
        actor_map = {"fb_page": actors["page_id"], "ig_pro": actors["ig_user_id"], "threads": actors["threads_user_id"]}
        tokens = {"page_token": os.environ.get("META_PAGE_ACCESS_TOKEN", ""), "threads_token": os.environ.get("THREADS_ACCESS_TOKEN", "")}
        return actors, actor_map, tokens

    def _prepare_campaign(self, args: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], List[str]]:
        adapted = adapt_campaign_copy(brief=args["brief"], repo_url=args["repo_url"], booking_url=args["booking_url"], keyword=args["keyword"], hook=args.get("hook"), topic_tag=args.get("topic_tag", "OpenSourceAI"), image=args.get("image"))
        cmp_row = self.store.create_campaign(title=args["title"], brief=args["brief"], keyword=args["keyword"], repo_url=args["repo_url"], booking_url=args["booking_url"], strict_public_passed=bool(args.get("strict_public_passed", True)))
        chs = args.get("channels") or (["fb_page", "threads"] if not args.get("image") else ["fb_page", "ig_pro", "threads"])
        return cmp_row, adapted, chs

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        dry = bool(args["dry_run"]) if "dry_run" in args else self.default_dry_run
        if name == "flywheel_adapt_copy":
            return adapt_campaign_copy(brief=args["brief"], repo_url=args["repo_url"], booking_url=args["booking_url"], keyword=args["keyword"], hook=args.get("hook"), topic_tag=args.get("topic_tag", "OpenSourceAI"), image=args.get("image"))
        if name == "flywheel_stage_campaign":
            cmp_row, adapted, chs = self._prepare_campaign(args)
            staged = stage_campaign_for_approval(store=self.store, campaign_id=cmp_row["campaign_id"], adapted_copy=adapted, channels=chs, staged_media_url=args.get("staged_media_url"))
            return {**staged, "status": "pending_human_approval", "approval_status": staged["status"], "campaign_id": cmp_row["campaign_id"], "adapted": adapted}
        if name == "flywheel_list_approvals":
            return {"approvals": list_approval_requests(self.store, status=args.get("status"), stage=args.get("stage"))}
        if name == "flywheel_publish_campaign":
            if not dry:
                raise HumanApprovalRequiredError("MCP agents cannot execute live publishing. Call flywheel_stage_campaign and approve via interactive CLI (--approve) or dashboard UI.")
            cmp_row, adapted, chs = self._prepare_campaign(args)
            actors, _, tokens = self._actors_and_tokens()
            return publish_campaign(store=self.store, campaign_id=cmp_row["campaign_id"], adapted_copy=adapted, actors=actors, tokens=tokens, channels=chs, dry_run=True)
        if name == "flywheel_retry_failed":
            if not dry:
                raise HumanApprovalRequiredError("MCP agents cannot execute live channel retries. Run --retry <job_id> from interactive CLI or dashboard UI.")
            actors, _, tokens = self._actors_and_tokens()
            return retry_failed_channels(store=self.store, job_id=args["job_id"], actors=actors, tokens=tokens, dry_run=True)
        if name == "flywheel_simulate_webhook":
            if not dry:
                raise HumanApprovalRequiredError("MCP agents cannot dispatch live webhook replies; set dry_run=true.")
            _, actor_map, tokens = self._actors_and_tokens()
            return process_comment_webhook(store=self.store, campaign_id=args["campaign_id"], platform=args["platform"], comment_id=args["comment_id"], author_id=args["author_id"], comment_text=args["comment_text"], actor_ids=actor_map, tokens=tokens, dry_run=True)
        if name == "flywheel_token_doctor":
            return self.doctor.run_doctor(self.store, dry_run=dry)
        raise ValueError(f"Unknown MCP tool: {name}")

    def approve_and_publish_tty(self, approval_id: str, *, dry_run: bool = False) -> Dict[str, Any]:
        rec = get_approval_request(self.store, approval_id)
        if rec["stage"] != "stage2_social":
            raise ValueError(f"Unsupported stage for social publisher: {rec['stage']}")
        pl = rec["payload"]
        actors, _, tokens = self._actors_and_tokens()
        if dry_run:
            return publish_campaign(store=self.store, campaign_id=pl["campaign_id"], adapted_copy=pl["channel_copy"], actors=actors, tokens=tokens, channels=pl["channels"], staged_media_url=pl.get("staged_media_url"), dry_run=True)
        grant = prompt_tty_human_approval(self.store, approval_id=approval_id)
        return publish_campaign(store=self.store, campaign_id=pl["campaign_id"], adapted_copy=pl["channel_copy"], actors=actors, tokens=tokens, channels=pl["channels"], staged_media_url=pl.get("staged_media_url"), approval=grant, dry_run=False)


def serve_stdio(server: FlywheelMcpServer) -> None:
    """Run JSON-RPC 2.0 MCP loop over stdin/stdout (protocolVersion 2024-11-05)."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req = json.loads(line)
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        resp: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if method == "initialize":
            resp["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "meta-flywheel-mcp", "version": "0.2.0"}}
        elif method == "tools/list":
            resp["result"] = {"tools": TOOL_SPECS}
        elif method == "tools/call":
            try:
                out = server.call_tool(params.get("name", ""), params.get("arguments") or {})
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}
            except Exception as exc:
                resp["result"] = {"content": [{"type": "text", "text": json.dumps({"error": str(exc)})}], "isError": True}
        else:
            resp["error"] = {"code": -32601, "message": f"Method not found: {method}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="meta-flywheel-mcp", description="Meta Graph, IG Pro & Threads Lead-Magnet Flywheel MCP Server")
    p.add_argument("--mcp", action="store_true", help="Start stdio JSON-RPC 2024-11-05 MCP server")
    p.add_argument("--list-tools", action="store_true", help="Print MCP tool schemas as JSON")
    p.add_argument("--doctor", action="store_true", help="Run Meta & Threads token health doctor")
    p.add_argument("--approve", metavar="APPROVAL_ID", help="Interactively review (/dev/tty) and publish a staged approval_id")
    p.add_argument("--retry", metavar="JOB_ID", help="Retry FAILED channels for an approved job_id")
    p.add_argument("--webhook-serve", action="store_true", help="Start `/events` HTTP webhook server")
    p.add_argument("--media-serve", action="store_true", help="Start `/stage` ephemeral media HTTP server")
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    p.add_argument("--port", type=int, default=8620, help="HTTP bind port")
    p.add_argument("--stage-dir", help="Directory for staged media assets")
    p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False, help="Enable/disable dry-run mode (--dry-run / --no-dry-run)")
    p.add_argument("--db", default=":memory:", help="SQLite path (default: :memory:)")
    args = p.parse_args(argv)
    srv = FlywheelMcpServer(db_path=args.db, default_dry_run=bool(args.dry_run))
    if args.list_tools:
        print(json.dumps(TOOL_SPECS, indent=2)); return 0
    if args.doctor:
        print(json.dumps(srv.call_tool("flywheel_token_doctor", {"dry_run": bool(args.dry_run)}), indent=2)); return 0
    if args.approve:
        print(json.dumps(srv.approve_and_publish_tty(args.approve, dry_run=bool(args.dry_run)), indent=2)); return 0
    if args.retry:
        actors, _, tokens = srv._actors_and_tokens()
        print(json.dumps(retry_failed_channels(store=srv.store, job_id=args.retry, actors=actors, tokens=tokens, dry_run=bool(args.dry_run)), indent=2)); return 0
    if args.webhook_serve:
        _, actor_map, tokens = srv._actors_and_tokens()
        serve_webhook(host=args.host, port=args.port, store=srv.store, actor_ids=actor_map, tokens=tokens, dry_run=bool(args.dry_run)); return 0
    if args.media_serve:
        serve_media_stage(host=args.host, port=args.port, stage_dir=args.stage_dir); return 0
    if args.mcp:
        serve_stdio(srv); return 0
    p.print_help(); return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Cynthia Schomp
"""CLI and stdio JSON-RPC 2024-11-05 MCP server for meta-flywheel-mcp."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .adapter import adapt_campaign_copy
from .auth import MetaAuthDoctor
from .db import FlywheelStore
from .publisher import publish_campaign, retry_failed_channels
from .servers import serve_media_stage, serve_webhook
from .webhook import process_comment_webhook


def _tool(name: str, desc: str, props: Dict[str, Any], req: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if req:
        schema["required"] = req
    return {"name": name, "description": desc, "inputSchema": schema}


TOOL_SPECS: List[Dict[str, Any]] = [
    _tool(
        "flywheel_adapt_copy",
        "Compile a master brief into FB Page, IG Professional, and Auto-Chained Threads payloads.",
        {"brief": {"type": "string"}, "repo_url": {"type": "string"}, "booking_url": {"type": "string"}, "keyword": {"type": "string"}, "hook": {"type": "string"}, "topic_tag": {"type": "string"}, "image": {"type": "object"}},
        ["brief", "repo_url", "booking_url", "keyword"],
    ),
    _tool(
        "flywheel_publish_campaign",
        "Verify public 200 OK gates and publish across FB Page, IG Pro, and Threads.",
        {"title": {"type": "string"}, "brief": {"type": "string"}, "repo_url": {"type": "string"}, "booking_url": {"type": "string"}, "keyword": {"type": "string"}, "strict_public_passed": {"type": "boolean"}, "channels": {"type": "array", "items": {"type": "string"}}, "dry_run": {"type": "boolean"}},
        ["title", "brief", "repo_url", "booking_url", "keyword"],
    ),
    _tool(
        "flywheel_retry_failed",
        "Retry only FAILED channels for a publish job while skipping PUBLISHED channels.",
        {"job_id": {"type": "string"}, "dry_run": {"type": "boolean"}},
        ["job_id"],
    ),
    _tool(
        "flywheel_simulate_webhook",
        "Simulate or process an inbound Meta comment webhook event with idempotency.",
        {"campaign_id": {"type": "string"}, "platform": {"type": "string"}, "comment_id": {"type": "string"}, "author_id": {"type": "string"}, "comment_text": {"type": "string"}, "dry_run": {"type": "boolean"}},
        ["campaign_id", "platform", "comment_id", "author_id", "comment_text"],
    ),
    _tool("flywheel_token_doctor", "Inspect Meta App LIVE readiness, Page/IG bindings, and 60-day Threads token age.", {"dry_run": {"type": "boolean"}}),
]


class FlywheelMcpServer:
    """Stateful MCP tool dispatcher backed by SQLite FlywheelStore."""

    def __init__(self, db_path: Optional[str] = None, default_dry_run: bool = False) -> None:
        self.store = FlywheelStore(db_path or os.environ.get("META_FLYWHEEL_DB") or ":memory:")
        self.default_dry_run = default_dry_run
        self.doctor = MetaAuthDoctor()

    def _actors_and_tokens(self) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        actors = {
            "page_id": os.environ.get("META_PAGE_ID", "page_dry"),
            "ig_user_id": os.environ.get("IG_USER_ID", "ig_dry"),
            "threads_user_id": os.environ.get("THREADS_USER_ID", "threads_dry"),
        }
        actor_map = {"fb_page": actors["page_id"], "ig_pro": actors["ig_user_id"], "threads": actors["threads_user_id"]}
        tokens = {"page_token": os.environ.get("META_PAGE_ACCESS_TOKEN", ""), "threads_token": os.environ.get("THREADS_ACCESS_TOKEN", "")}
        return actors, actor_map, tokens

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        dry = bool(args["dry_run"]) if "dry_run" in args else self.default_dry_run
        if name == "flywheel_adapt_copy":
            return adapt_campaign_copy(
                brief=args["brief"], repo_url=args["repo_url"], booking_url=args["booking_url"],
                keyword=args["keyword"], hook=args.get("hook"), topic_tag=args.get("topic_tag", "OpenSourceAI"), image=args.get("image"),
            )
        if name == "flywheel_publish_campaign":
            adapted = adapt_campaign_copy(
                brief=args["brief"], repo_url=args["repo_url"], booking_url=args["booking_url"],
                keyword=args["keyword"], hook=args.get("hook"), topic_tag=args.get("topic_tag", "OpenSourceAI"), image=args.get("image"),
            )
            cmp_row = self.store.create_campaign(
                title=args["title"], brief=args["brief"], keyword=args["keyword"],
                repo_url=args["repo_url"], booking_url=args["booking_url"], strict_public_passed=bool(args.get("strict_public_passed", True)),
            )
            actors, _, tokens = self._actors_and_tokens()
            chs = args.get("channels") or (["fb_page", "threads"] if not args.get("image") else ["fb_page", "ig_pro", "threads"])
            return publish_campaign(store=self.store, campaign_id=cmp_row["campaign_id"], adapted_copy=adapted, actors=actors, tokens=tokens, channels=chs, dry_run=dry)
        if name == "flywheel_retry_failed":
            actors, _, tokens = self._actors_and_tokens()
            return retry_failed_channels(store=self.store, job_id=args["job_id"], actors=actors, tokens=tokens, dry_run=dry)
        if name == "flywheel_simulate_webhook":
            _, actor_map, tokens = self._actors_and_tokens()
            return process_comment_webhook(
                store=self.store, campaign_id=args["campaign_id"], platform=args["platform"],
                comment_id=args["comment_id"], author_id=args["author_id"], comment_text=args["comment_text"],
                actor_ids=actor_map, tokens=tokens, dry_run=dry,
            )
        if name == "flywheel_token_doctor":
            return self.doctor.run_doctor(self.store, dry_run=dry)
        raise ValueError(f"Unknown MCP tool: {name}")


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
            resp["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "meta-flywheel-mcp", "version": "0.1.0"}}
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
        print(json.dumps(TOOL_SPECS, indent=2))
        return 0
    if args.doctor:
        print(json.dumps(srv.call_tool("flywheel_token_doctor", {"dry_run": bool(args.dry_run)}), indent=2))
        return 0
    if args.webhook_serve:
        _, actor_map, tokens = srv._actors_and_tokens()
        serve_webhook(host=args.host, port=args.port, store=srv.store, actor_ids=actor_map, tokens=tokens, dry_run=bool(args.dry_run))
        return 0
    if args.media_serve:
        serve_media_stage(host=args.host, port=args.port, stage_dir=args.stage_dir)
        return 0
    if args.mcp:
        serve_stdio(srv)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

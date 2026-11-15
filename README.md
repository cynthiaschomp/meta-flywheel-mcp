# meta-flywheel-mcp — Deterministic Facebook, Instagram & Threads Lead-Magnet Engine + MCP Server

> **`meta-flywheel-mcp`** is a zero-dependency Python engine and Model Context Protocol (MCP) server for staging technical stories across Facebook Pages, Instagram Professional, and Threads with mandatory human-in-the-loop SHA-256 publishing approval and idempotent Meta Private Reply DM delivery. Agents stage drafts via MCP while only interactive human TTY or UI sign-off can release live posts (`0 MB VRAM` overhead).

<p align="center">
  <img src="docs/assets/readme-header.svg" width="100%" alt="meta-flywheel-mcp — Deterministic Facebook, Instagram & Threads Lead-Magnet Engine + MCP Server">
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &ensp;·&ensp;
  <a href="#documentation">Documentation</a> &ensp;·&ensp;
  <a href="#why-it-exists">Why It Exists</a> &ensp;·&ensp;
  <a href="#comparison">Comparison</a> &ensp;·&ensp;
  <a href="#architecture">Architecture</a> &ensp;·&ensp;
  <a href="#benchmarks">Benchmarks</a> &ensp;·&ensp;
  <a href="#faq">FAQ</a> &ensp;·&ensp;
  <a href="#who-built-it">Who Built It</a> &ensp;·&ensp;
  <a href="#feedback">Feedback</a>
</p>

<p align="center">![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg) ![Runtime: python](https://img.shields.io/badge/runtime-python-success.svg) ![Dependencies: 0](https://img.shields.io/badge/dependencies-zero-brightgreen.svg) ![MCP: Ready](https://img.shields.io/badge/MCP-server.json-orange.svg)</p>

---

## Quick Start

**No terminal wrestling required:** click GitHub's **Copy** button on the top-right of any snippet below, or hand the single prompt in Step 1 directly to your AI coding agent (**Claude Code, Cursor, Antigravity, Codex, or OpenCode**) and let it handle setup end-to-end via [`llms.txt`](./llms.txt).

### 1. Let Your AI Agent Install It For You (1-Click Copy)

```text
Install and configure https://github.com/cynthiaschomp/meta-flywheel-mcp in this project using its llms.txt and docs/getting-started.md, then run a quick check to confirm everything works.
```

### 2. Terminal Install (1-Click Copy)

```sh
git clone https://github.com/cynthiaschomp/meta-flywheel-mcp.git && cd meta-flywheel-mcp && python3 -m meta_flywheel.mcp_server --doctor --dry-run
```

### 3. 5-Line MCP Config (Claude Desktop, Cursor, Antigravity, OpenCode)

```json
{
  "mcpServers": {
    "meta-flywheel-mcp": {
      "command": "python3",
      "args": [
        "-m",
        "meta_flywheel.mcp_server",
        "--mcp",
        "--dry-run"
      ]
    }
  }
}
```

### 4. Programmatic SDK / CLI Usage

```python
from meta_flywheel import adapt_campaign_copy

payload = adapt_campaign_copy(
    brief="Our dashboard claimed 4.2x ROAS while net cash dropped by $14,200.",
    repo_url="https://github.com/cynthiaschomp/roas-to-cash-mcp",
    booking_url="https://cynthiaschomp.com/book",
    keyword="ROAS",
)
print(payload["threads"]["post_1_bytes"], payload["threads"]["post_2"])
```

## Documentation

- **[Getting Started & Full Setup Guide](./docs/getting-started.md)** — Step-by-step installation, configuration, and examples
- **[AI Agent Context (`llms.txt`)](./llms.txt)** — Single-fetch `<1,500`-token SDK, CLI & architecture reference
- **[MCP Registry Manifest (`server.json`)](./server.json)** — Official `stdio` tool & registry metadata

<a id="why-it-exists"></a>

## Why Technical Founders & AI Workflows Need This

- **Link suppression & Threads byte-count failures** — Keeps feed posts link-free, enforces exact UTF-8 byte counting (`<= 500` bytes across multi-byte emojis), strips 'link in bio' or 'DM me' bait, and auto-chains links in Thread #2 via `reply_to_id`.
- **Duplicate posts on partial API timeouts** — Tracks `PENDING -> STAGED -> CONTAINER_CREATED -> PUBLISHED | FAILED` per channel in SQLite so `retry_failed_channels` retries only failed surfaces without double-posting.
- **Infinite bot reply loops & multi-bubble DM lockouts** — Drops self-authored webhook events immediately, deduplicates `(platform, comment_id)` and `(campaign_id, commenter_id)`, and combines repo + booking URLs into one atomic Private Reply bubble.

## Comparison

| Feature | `meta-flywheel-mcp` | ManyChat | Buffer | Zapier |
| :--- | :--- | :--- | :--- | :--- |
| **Auto-chained Threads `reply_to_id` + UTF-8 byte gate** | **Yes (Built-in)** | No | Partial | Manual multi-zap |
| **Atomic 1-bubble Comment-to-DM (`comment_id`)** | **Yes (SQLite idempotent)** | Per-contact SaaS tier | No | Brittle webhook glue |
| **Channel-isolated retry without double-posting** | **Yes (`PUBLISHED` skipped)** | N/A | Manual re-queue | Replays whole run |
| **Self-hosted MCP server (`0` external deps)** | **Yes (Apache-2.0)** | Closed SaaS | Closed SaaS | Closed SaaS |

## Architecture

```text
Master Brief + Public URLs -> [MCP Draft Stager + SHA-256 Lock] -> [Human TTY/UI Final Approval Gate] -> [SQLite Job Spine] -> FB / IG / Threads #1+#2 -> [HMAC Webhook + Self-Author Breaker] -> Atomic Private Reply DM
```

## Benchmarks

| Metric | Measured Value | Conditions |
| :--- | :--- | :--- |
| Webhook HMAC-SHA256 + SQLite idempotency check | `0.42ms` | Constant-time `hmac.compare_digest` + indexed SQLite lookup (`<50ms` Meta SLA) |
| Background daemon / GPU VRAM footprint | `0 MB VRAM` | Pure Python 3.10+ stdlib + embedded SQLite (`~14 MB` RSS) |
| MCP tool schema + `llms.txt` context footprint | `920 tokens` | Fits cleanly inside `<1,500` tokens for zero-bloat agent discovery |

## FAQ

### How do I connect meta-flywheel-mcp to Claude Code, Cursor, or another MCP client?

Add the 5-line `mcpServers` block in Quick Start pointing to `python3 -m meta_flywheel.mcp_server --mcp --dry-run`, and export `META_APP_ID` plus `META_APP_SECRET` in your environment when switching to live Graph API calls.

### How do I prevent duplicate Facebook or Instagram posts when Threads times out?

Run `meta-flywheel-mcp --db flywheel.db --retry <job_id>` (or `retry_failed_channels(store, job_id=...)` in Python; `flywheel_retry_failed` previews retries in MCP dry-run mode). Every channel transitions independently in SQLite, and any surface already in `PUBLISHED` is strictly skipped.

### How do I avoid Meta's 24-hour messaging lockout when sending both a GitHub link and booking link?

Meta allows only one unsolicited Private Reply message per `comment_id` until the user responds. `build_atomic_private_reply_payload` combines both the GitHub repository link and booking link into a single atomic DM bubble alongside a public Message Requests nudge.

---

## Who Built It

<img src="https://github.com/cynthiaschomp.png?size=264" width="124" align="left" alt="Cynthia Schomp">

**Cynthia Schomp**

I run a web design, marketing, and AI systems studio—building practical software, agent infrastructure, and developer tools I need in production first, and sharing reusable building blocks openly so other builders can move faster too.

**Connect:** [GitHub](https://github.com/cynthiaschomp) &ensp;·&ensp; [Website](https://cynthiaschomp.com) &ensp;·&ensp; [Contact](https://cynthiaschomp.com/contact/)

<br clear="left">

---

## Feedback

Questions, bug reports, benchmark comparisons, or ideas for what you need next? [Open an issue](https://github.com/cynthiaschomp/meta-flywheel-mcp/issues) or start a thread in [Discussions](https://github.com/cynthiaschomp/meta-flywheel-mcp/discussions)—real-world developer feedback directly shapes the roadmap.

License: [Apache-2.0](./LICENSE) · Attribution required via [`NOTICE`](./NOTICE)

# Getting Started with `meta-flywheel-mcp`

> Zero-dependency Python MCP server for Meta Graph, Instagram Pro & Threads publishing, auto-chained replies, and idempotent Comment-to-DM delivery.

## 1. Zero-Friction AI Agent Setup

Paste this into Claude Code, Cursor, Antigravity, Codex, or OpenCode:

```text
Install and configure https://github.com/cynthiaschomp/meta-flywheel-mcp using its llms.txt and docs/getting-started.md, then run a verification check.
```

## 2. Terminal Install

```sh
git clone https://github.com/cynthiaschomp/meta-flywheel-mcp.git && cd meta-flywheel-mcp && python3 -m meta_flywheel.mcp_server --doctor --dry-run
```

## 3. Programmatic / CLI Usage

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

## 4. Machine-Readable References

- [`../llms.txt`](../llms.txt) — `<1,500`-token full SDK & architecture cheat sheet
- [`../server.json`](../server.json) — Official MCP Registry manifest

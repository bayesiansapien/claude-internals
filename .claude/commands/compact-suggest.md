---
description: Full prefix anatomy + projection + verdict — should you /compact now, and what will it save?
---

Run the compact-suggest script and present the output verbatim:

```bash
python3 claude-optimizer/scripts/compact_suggest.py
```

This is the flagship optimizer view. It returns:
  - Prefix size + % of context window + current $/turn burn rate
  - ANATOMY: where every prefix token lives (system + conversation, ranked)
  - PROJECTION: growth rate, headroom, economic savings if /compact NOW
  - BOUNDARY ANALYSIS: T1 deterministic + T2 Haiku tiebreaker + T3 Sonnet judge
  - VERDICT: COMPACT_NOW | SOON | WAIT | NO_ACTION + reason

Present the output verbatim. If the verdict is COMPACT_NOW or SOON, ask the
user if they want to run /compact now. If the verdict is WAIT (Sonnet judge
veto), don't push — just relay the reason.

For drill-downs the user can run separately:
  - System row "MCP schemas" → /mcp-audit
  - System row "Auto-memory" → /memory-hygiene
  - System row "CLAUDE.md" → user edits CLAUDE.md files directly

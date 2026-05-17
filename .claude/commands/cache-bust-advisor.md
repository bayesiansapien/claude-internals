---
description: Proactive recommendation on a planned cache-busting change (model switch, CLAUDE.md edit, memory edit, MCP change). Returns GO/WAIT/URGENT verdict + cost.
---

The user has typed `/cache-bust-advisor <description>`. The description is
free-text describing what they're planning to change (e.g., "switch to sonnet",
"edit CLAUDE.md to tighten output style", "add to memory file").

Run the cache-bust-advisor script, passing the description as args:

```bash
python3 claude-optimizer/scripts/cache_bust_advisor.py "<description>"
```

Present the output verbatim. The script returns:
  - Classified action type (model_switch / claude_md_edit / memory_edit / mcp_change)
  - Current cached prefix + rebuild cost estimate
  - Boundary signals (reasons to WAIT vs reasons it's OK)
  - VERDICT: GO | WAIT | MIXED | URGENT + reason
  - Recent bust history this session

If the verdict is GO or URGENT, suggest the user proceed.
If WAIT, advise finishing current work first.
If MIXED, lay out the trade-offs and let them decide.

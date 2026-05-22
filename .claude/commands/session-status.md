---
description: Show the current session's budget, token usage, phase, compactions, and recent advisor activity. On-demand inspection without triggering the boundary advisor.
---

Run the session-status script and present the output verbatim:

```bash
python3 claude-optimizer/scripts/session_status.py
```

The script returns:
  - Current session UUID + project
  - Token usage: total (budget-relevant) + cache reads + output
  - Budget: limit + ratio used + remaining
  - Phase detection: current + predicted next + tool breakdown
  - Compaction count
  - Recent advisor activity (how many recommendations, last fire timestamp)

This is informational only. It does NOT trigger the advisor banner.

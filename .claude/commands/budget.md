---
description: Set or show the per-session token budget that drives the session-boundary-advisor. Usage: /budget 5M or /budget reset or /budget (no args to show current).
---

The user has typed `/budget [<value>]`. The argument is optional.

Possible argument forms:
  - empty → show current budget and session state
  - `5M`, `2M`, `10M`, etc. → set budget to that many million tokens
  - `3000000`, `2500000` → set budget to that exact token count
  - `reset` → revert to the default from the environment / settings

Run the budget script with the provided argument:

```bash
python3 claude-optimizer/scripts/session_budget.py "<argument-or-empty>"
```

Present the output verbatim. The script will return one of:
  - Current budget state (when no arg)
  - Confirmation of new budget (when set)
  - Error message if the argument is invalid

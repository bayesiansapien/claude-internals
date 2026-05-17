---
description: Per-source cost breakdown for the current Claude Code session
---

Run the cost-snapshot script and present the output to the user.

```bash
python3 claude-optimizer/scripts/cost_snapshot.py
```

If the user passes `--all`, run with `--all` to aggregate across all sessions in this project.
If the user passes a session UUID, pass it as `--session <uuid>`.

Present the output verbatim — it's already formatted. Do not summarize unless explicitly asked.

#!/usr/bin/env python3
"""session_budget · backend for the /budget slash command.

Usage:
    python3 session_budget.py            → show current budget
    python3 session_budget.py 5M         → set budget to 5M tokens
    python3 session_budget.py 2500000    → set budget to 2.5M tokens
    python3 session_budget.py reset      → revert to default
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.session_budget_state import (
    get_session, set_budget,
    cumulative_session_tokens, DEFAULT_BUDGET,
)
from lib.hook_payload import resolve_session


def _parse_budget(arg):
    """Parse a budget argument. Returns int tokens or None on failure."""
    if not arg:
        return None
    s = arg.strip().lower().replace(",", "").replace("_", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mk]?)$", s)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix == "m":
        val *= 1_000_000
    elif suffix == "k":
        val *= 1_000
    return int(val)


def _format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    # Slash commands have no stdin payload; resolve_session falls back to
    # CC_SESSION_ID env var (if CC sets it) then mtime-based discovery.
    session_uuid, jsonl_path = resolve_session()
    if not session_uuid:
        print("⚠ No Claude Code session detected in this directory.")
        print(f"   (Looked under ~/.claude/projects/{os.getcwd().replace('/','-')}/)")
        sys.exit(0)

    state = get_session(session_uuid)
    current = state.get("budget_tokens", DEFAULT_BUDGET)

    if arg == "" or arg.lower() == "show":
        usage = cumulative_session_tokens(jsonl_path) if jsonl_path else {"total": 0}
        used = usage.get("total", 0)
        ratio = used / current if current else 0
        print(f"  SESSION BUDGET · {session_uuid[:8]}…")
        print(f"  ─────────────────────────────────────────────────────")
        print(f"  Budget:        {_format_tokens(current)} tokens")
        print(f"  Used so far:   {_format_tokens(used)} ({ratio*100:.1f}%)")
        print(f"  Remaining:     {_format_tokens(max(0, current - used))}")
        print(f"  Compactions:   {state.get('compactions', 0)}")
        print(f"  Last phase:    {state.get('last_phase', '—')}")
        print()
        print(f"  Override:      /budget 5M    (or any value with K/M suffix)")
        print(f"  Reset:         /budget reset")
        return

    if arg.lower() == "reset":
        env_default = int(os.environ.get("CC_SESSION_TOKEN_LIMIT", str(DEFAULT_BUDGET)))
        set_budget(session_uuid, env_default)
        print(f"✓ Budget reset to default: {_format_tokens(env_default)} tokens")
        return

    new_val = _parse_budget(arg)
    if new_val is None:
        print(f"⚠ Could not parse budget value: '{arg}'")
        print(f"  Examples: 5M, 2.5M, 10M, 3000000, reset")
        sys.exit(1)

    if new_val < 100_000:
        print(f"⚠ Budget too low ({_format_tokens(new_val)}). Minimum is 100K.")
        sys.exit(1)

    set_budget(session_uuid, new_val)
    print(f"✓ Budget updated to {_format_tokens(new_val)} tokens")
    print(f"  (was {_format_tokens(current)})")


if __name__ == "__main__":
    main()

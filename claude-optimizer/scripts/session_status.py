#!/usr/bin/env python3
"""session_status · backend for /session-status slash command.

Read-only inspection of the current session's budget, phase, compactions,
and recent advisor activity. Does NOT trigger the boundary advisor banner.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.session_budget_state import (
    get_session, cumulative_session_tokens, count_compactions_in_jsonl,
    next_enumerated_name,
)
from lib.hook_payload import resolve_session
from lib.phase_detector import detect_phase, predict_next_phase, generate_session_name


def _format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _format_ts(ts):
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def main():
    session_uuid, jsonl_path = resolve_session()
    if not session_uuid:
        print("⚠ No session detected in this directory.")
        sys.exit(0)

    state = get_session(session_uuid)
    usage = cumulative_session_tokens(jsonl_path) if jsonl_path else {}
    compactions = count_compactions_in_jsonl(jsonl_path) if jsonl_path else 0
    phase, conf, breakdown = detect_phase(jsonl_path) if jsonl_path else ("unknown", 0, {})
    next_phase = predict_next_phase(phase, breakdown)
    cwd = os.getcwd()
    project_name = Path(cwd).name
    current_session_name = state.get("session_name") or project_name
    enumerated = next_enumerated_name(cwd)
    name_options = generate_session_name(
        phase, next_phase, project_name=project_name,
        current_session_name=current_session_name,
        enumerated_default=enumerated,
    )

    budget = state.get("budget_tokens", 3_000_000)
    used = usage.get("total", 0)
    ratio = used / budget if budget else 0

    print(f"  SESSION STATUS · {session_uuid[:8]}…")
    print(f"  ═══════════════════════════════════════════════════════════")
    print()
    print(f"  Project:           {project_name}")
    print(f"  Session name:      {current_session_name}")
    print(f"  Session UUID:      {session_uuid}")
    print()
    print(f"  TOKEN USAGE  (budget = cache writes + output + fresh input)")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  Used / budget:     {_format_tokens(used)} / {_format_tokens(budget)} ({ratio*100:.1f}%)")
    print(f"  Cache reads:       {_format_tokens(usage.get('cache_reads', 0))}  (informational)")
    print(f"  Output:            {_format_tokens(usage.get('output', 0))}")
    print(f"  Fresh input:       {_format_tokens(usage.get('fresh_input', 0))}")
    print()
    print(f"  PHASE DETECTION  (last 20 turns)")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  Current phase:     {phase}  (confidence {conf})")
    print(f"  Predicted next:    {next_phase or '—'}")
    if breakdown:
        print(f"  Tool distribution:")
        for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1]):
            print(f"    {k:18s} {v}")
    print()
    print(f"  COMPACTIONS")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  This session:      {compactions}")
    print(f"  Quality risk:      {'⚠ high (5+)' if compactions >= 5 else '✓ low'}")
    print()
    print(f"  ADVISOR STATE")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  Recommendations fired: {state.get('boundary_recommendations_fired', 0)}")
    print(f"  Last fire:             {_format_ts(state.get('last_recommendation_ts'))}")
    print(f"  User declined launch:  {state.get('user_declined_launch', False)}")
    print()
    print(f"  IF SPLIT NOW")
    print(f"  ────────────────────────────────────────────────────")
    print(f"  Default name:      {name_options['default']}")
    if name_options.get('suggestions'):
        print(f"  Task-based options:")
        for i, s in enumerate(name_options['suggestions'], 1):
            print(f"    {i}. {s}")
    print(f"  Use /budget to adjust the budget; the advisor will fire when")
    print(f"  conditions cross the threshold + a clean boundary is detected.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""cache-bust-advisor · Proactive recommendation on a planned cache-busting change.

Usage:
  /cache-bust-advisor <description of planned change>

Examples:
  /cache-bust-advisor switch to sonnet
  /cache-bust-advisor edit CLAUDE.md to tighten output style
  /cache-bust-advisor add new entry to memory/user_profile.md

Inputs:
  - User's free-text description (sys.argv joined)

Outputs:
  - Current cached prefix + rebuild cost estimate
  - Boundary signals (same as cache_bust_warner)
  - Verdict: GO | WAIT | URGENT (with reason)
  - History of recent bust events for context
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.anatomy import compute_anatomy, get_oauth_token, get_model_rates
from lib.task_hierarchy import (
    get_latest_session_path, load_session, extract_signals,
)
from lib.cache_bust_state import all_events_for_session


# Classify the planned change from free-text input
def classify_input(text):
    t = (text or "").lower()
    if any(k in t for k in ("model switch", "switch to ", "/model", "change model",
                              "use sonnet", "use opus", "use haiku")):
        return "model_switch"
    if "claude.md" in t or "claudemd" in t or "system instruction" in t:
        return "claude_md_edit"
    if any(k in t for k in ("memory", "auto-memory", "user_profile", "feedback_")):
        return "memory_edit"
    if any(k in t for k in ("mcp", "connector", "disconnect mcp")):
        return "mcp_change"
    return "generic"


ACTION_NOTES = {
    "model_switch": (
        "Model swap = totally fresh KV cache. Best done at a clear handoff "
        "(planning → implementation, etc.)."
    ),
    "claude_md_edit": (
        "Top-level instructions change. The new rules apply to NEW turns after "
        "cache rebuilds — so do this BEFORE a new task starts."
    ),
    "memory_edit": (
        "Memory affects this and future sessions. If mid-debugging, finish "
        "first so memory captures the resolved state."
    ),
    "mcp_change": (
        "MCP changes alter the tool schema set in the system prompt — full "
        "cache rebuild on next call."
    ),
    "generic": "",
}


def fmt_money(usd):
    if usd is None: return "—"
    if abs(usd) >= 1: return f"${usd:.2f}"
    return f"${usd:.3f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("description", nargs="*",
                        help="Free-text description of planned cache-busting change")
    args = parser.parse_args()
    description = " ".join(args.description).strip()

    print()
    print("  CACHE-BUST ADVISOR")
    print("  " + "═" * 72)
    if not description:
        print("  Usage: /cache-bust-advisor <description of planned change>")
        print("  Example: /cache-bust-advisor switch to sonnet")
        print()
        return

    action_type = classify_input(description)
    print(f"  Planned change:  {description}")
    print(f"  Classified as:   {action_type.replace('_', ' ')}")
    print()

    # Current state
    token = get_oauth_token()
    anatomy = compute_anatomy(token)
    prefix = anatomy["total_prefix"]
    model = anatomy.get("model") or "unknown"

    # Rebuild cost — model-aware via get_model_rates()
    rates = get_model_rates(model)
    rebuild_cost = prefix / 1e6 * rates["cache_write"]
    full_rebuild = prefix / 1e6 * rates["input"]

    print(f"  Current cached prefix:  {prefix:,} tokens ({anatomy['pressure_pct']:.1f}% of window)")
    print(f"  Model:                  {model}")
    print(f"  Rebuild penalty (cache_write rate): {fmt_money(rebuild_cost)}")
    print(f"  Worst-case (no cache write): {fmt_money(full_rebuild)}")
    print()

    # Boundary signals
    sess = get_latest_session_path()
    entries = load_session(sess) if sess else []
    s = extract_signals(entries) if entries else {}

    # Build a detailed signal map: each signal -> (fired?, explanation)
    signal_details = []

    n3 = s.get("tools_last_3_turns", 0)
    signal_details.append((
        "Rapid tool activity (WAIT)",
        n3 >= 15,
        f"tools in last 3 turns = {n3}; threshold = 15. "
        f"{'OVER threshold → mid-implementation, defer.' if n3 >= 15 else 'under threshold → not blocked by this.'}"
    ))

    err = bool(s.get("recent_tool_errors"))
    signal_details.append((
        "Unresolved tool errors (WAIT)",
        err,
        "is_error=true in tool_result in last 3 turns" if err
        else "no tool errors detected in last 3 turns",
    ))

    nfr = bool(s.get("new_file_read_recently"))
    signal_details.append((
        "New file just read (WAIT)",
        nfr,
        "files read in last 3 turns not seen earlier" if nfr
        else "no new file reads in last 3 turns",
    ))

    nsl = bool(s.get("new_skill_loaded_recently"))
    signal_details.append((
        "Skill loaded recently (WAIT)",
        nsl,
        "skill(s) loaded via Skill tool in last 5 turns" if nsl
        else "no skills loaded recently",
    ))

    tools_now = s.get("tools_this_turn", 0)
    signal_details.append((
        "Discussion turn (GO)",
        tools_now == 0,
        f"tools_this_turn = {tools_now}; "
        f"{'no tools used = clean break point' if tools_now == 0 else 'tools in use this turn → not a break point'}"
    ))

    ts = bool(s.get("topic_shift"))
    ts_ev = s.get("topic_shift_evidence", {})
    if ts_ev:
        ts_desc = (
            f"overlap with last {ts_ev.get('compared_against', '5 prompts')}: "
            f"{ts_ev.get('overlap_pct', 0)}% (threshold < 20%). "
            f"Current keywords: {', '.join(ts_ev.get('current_keywords', [])[:8])}. "
            f"Recent: {', '.join(ts_ev.get('recent_keywords_sample', [])[:10])}."
        )
    else:
        ts_desc = "insufficient prompt history"
    signal_details.append((
        "Topic shift (GO)",
        ts,
        ts_desc,
    ))

    vs = bool(s.get("velocity_slowing"))
    signal_details.append((
        "Velocity slowing (GO)",
        vs,
        "input_tokens growth slowing across last 4 turns (last delta < 60% of earliest)" if vs
        else "growth rate steady or accelerating",
    ))

    idle = s.get("idle_min", 0) or 0
    signal_details.append((
        "Idle pause (GO)",
        idle > 5,
        f"{idle:.0f} min since last user message; threshold > 5 min. "
        f"{'OVER → user is between tasks' if idle > 5 else 'under → user is active'}"
    ))

    reasons_wait = [(name, expl) for name, fired, expl in signal_details
                     if fired and "(WAIT)" in name]
    reasons_go = [(name, expl) for name, fired, expl in signal_details
                   if fired and "(GO)" in name]
    not_fired = [(name, expl) for name, fired, expl in signal_details if not fired]

    print("  BOUNDARY ANALYSIS  (every signal explained)")
    print("  " + "─" * 72)

    if reasons_go:
        print("  ✓ FIRED — Reasons it's OK:")
        for name, expl in reasons_go:
            print(f"      • {name.replace(' (GO)', '')}")
            print(f"        {expl}")
        print()

    if reasons_wait:
        print("  ⚠ FIRED — Reasons to WAIT:")
        for name, expl in reasons_wait:
            print(f"      • {name.replace(' (WAIT)', '')}")
            print(f"        {expl}")
        print()

    if not_fired:
        print("  ◌ NOT fired (here's why):")
        for name, expl in not_fired:
            short_name = name.replace(' (WAIT)', '').replace(' (GO)', '')
            print(f"      • {short_name}: {expl}")
        print()

    note = ACTION_NOTES.get(action_type, "")
    if note:
        print(f"  Action context: {note}")
        print()

    # Verdict — weighed by signal count + criticality
    n_wait = len(reasons_wait)
    n_go = len(reasons_go)
    if n_wait > n_go:
        verdict, glyph = "WAIT", "⚠"
        rec_reason = f"{n_wait} WAIT signal(s) > {n_go} GO signal(s); active work in progress"
    elif n_go > n_wait and n_wait == 0:
        verdict, glyph = "GO", "✓"
        rec_reason = f"{n_go} GO signal(s), 0 WAIT signals — clean boundary"
    elif n_go > 0 and n_wait > 0:
        verdict, glyph = "MIXED", "◐"
        rec_reason = f"{n_go} GO vs {n_wait} WAIT — your call based on the signals above"
    else:
        verdict, glyph = "OK", "ℹ"
        rec_reason = "no strong signal in either direction"

    # Override if pressure is critical
    if anatomy["pressure_pct"] >= 90 and action_type != "model_switch":
        verdict, glyph = "URGENT", "🚨"
        rec_reason = f"pressure at {anatomy['pressure_pct']:.0f}% — compact first, THEN bust"

    print("  VERDICT")
    print("  " + "═" * 72)
    print(f"  {glyph} {verdict}: {rec_reason}")
    print()

    # History
    history = all_events_for_session(str(sess)) if sess else []
    if history:
        print("  RECENT BUST HISTORY (this session)")
        print("  " + "─" * 72)
        for ev in history[-3:]:
            tstr = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
            print(f"    {tstr} · {ev.get('action_type', '?'):14s} · "
                  f"rebuild ~{fmt_money(ev.get('actual_rebuild_cost') or ev.get('estimated_rebuild_cost'))} · "
                  f"recovered {fmt_money(ev.get('post_bust_accumulated_cost'))} "
                  f"over {ev.get('post_bust_turn_count', 0)} user turns")
        print()


if __name__ == "__main__":
    main()

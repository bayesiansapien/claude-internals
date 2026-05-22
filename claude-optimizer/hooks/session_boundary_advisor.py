#!/usr/bin/env python3
"""session-boundary-advisor · Stop hook.

Monitors cumulative session tokens + phase + compactions + boundary signals.
When thresholds line up favorably, surfaces a recommendation banner asking
the user whether to split into a fresh session.

Decision matrix (high level):
  tokens < 70% budget       → silent
  tokens 70-90% + clean boundary → info banner
  tokens >= 90% + clean boundary → recommend split (offer auto-launch)
  tokens >= 90% + active boundary → warn but defer to clean break
  compactions >= 5          → quality warning regardless of tokens

The hook never blocks the user. It either stays silent or prints a banner.
The /budget slash command and the launcher script are separate triggers.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.session_budget_state import (
    get_session, cumulative_session_tokens,
    count_compactions_in_jsonl, record_recommendation_fired, can_fire_recommendation,
    record_phase, next_enumerated_name,
)
from lib.hook_payload import read_hook_payload, resolve_session
from lib.phase_detector import detect_phase, predict_next_phase, generate_session_name
from lib.task_hierarchy import get_latest_session_path, load_session, extract_signals
from lib.handoff_writer import write_handoff


# Thresholds (configurable via env vars)
WARN_THRESHOLD = float(os.environ.get("CC_SESSION_WARN_THRESHOLD", "0.7"))      # 70%
SPLIT_THRESHOLD = float(os.environ.get("CC_SESSION_SPLIT_THRESHOLD", "0.9"))    # 90%
COMPACTION_QUALITY_LIMIT = int(os.environ.get("CC_SESSION_COMPACTION_LIMIT", "5"))


def _format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _project_name_from_cwd(cwd):
    """Get a short project name (last path segment) for use in session name."""
    return Path(cwd).name or "session"


def _extract_recent_user_messages(jsonl_path, n=3):
    """Pull the last N text-only user messages from the session JSONL.

    Skips tool_result-bearing user entries (they aren't real prompts).
    """
    if not Path(jsonl_path).exists():
        return []
    msgs = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user" or d.get("isSidechain"):
                    continue
                m = d.get("message", {})
                content = m.get("content") if isinstance(m, dict) else None
                text = None
                if isinstance(content, str) and not d.get("toolUseResult"):
                    text = content
                elif isinstance(content, list):
                    has_tool = any(isinstance(b, dict) and b.get("type") == "tool_result"
                                   for b in content)
                    if not has_tool:
                        texts = [b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        if texts:
                            text = " ".join(texts)
                if text and text.strip():
                    msgs.append(text.strip())
    except Exception:
        return []
    return msgs[-n:]


def _boundary_is_clean(signals):
    """A 'clean' boundary = no in-progress work, no recent tool errors,
    no rapid implementation activity."""
    if signals.get("tools_last_3_turns", 0) >= 15:
        return False
    if signals.get("recent_tool_errors"):
        return False
    if signals.get("in_progress_task"):
        return False
    return True


def _build_banner(*, level, tokens_used, budget, ratio, compactions, phase_info,
                   boundary_clean, name_options, handoff_path=None,
                   current_session_name=None):
    """Construct the banner text based on level (info / split / quality)."""
    lines = []
    if level == "info":
        lines.append("⚠ SESSION BOUNDARY ADVISOR · approaching budget")
    elif level == "split":
        lines.append("🚨 SESSION BOUNDARY ADVISOR · split recommended")
    elif level == "quality":
        lines.append("⚠ SESSION QUALITY ADVISOR · compactions accumulating")
    else:
        lines.append("ℹ SESSION BOUNDARY ADVISOR")

    lines.append("")
    lines.append(f"   Tokens used:   {_format_tokens(tokens_used)} / {_format_tokens(budget)} "
                 f"({ratio*100:.0f}%)")
    lines.append(f"   Compactions:   {compactions}")
    lines.append(f"   Boundary:      {'clean (good moment to split)' if boundary_clean else 'active (mid-task)'}")

    if phase_info:
        cur = phase_info.get("current", "unknown")
        nxt = phase_info.get("next")
        conf = phase_info.get("confidence", 0)
        lines.append("")
        lines.append("   📍 PHASE DETECTION")
        lines.append(f"      Current:    {cur} (confidence {conf})")
        if nxt:
            lines.append(f"      Next:       {nxt} (predicted)")

    if level == "split":
        default_name = name_options["default"]
        suggestions = name_options.get("suggestions", [])
        lines.append("")
        lines.append("   📦 HANDOFF READY")
        if current_session_name:
            lines.append(f"      Current session: {current_session_name}")
        lines.append("")
        lines.append(f"   📛 NEW SESSION NAME")
        lines.append(f"      Default (enumerated): {default_name}")
        if suggestions:
            lines.append(f"      Task-based suggestions:")
            for i, s in enumerate(suggestions, 1):
                lines.append(f"        {i}. {s}")
        lines.append("")
        if handoff_path:
            lines.append(f"      Intent file:     {handoff_path}")
            lines.append("")
        lines.append("   To launch with the enumerated default, just say 'launch'.")
        lines.append("   To launch with a suggestion, say 'launch with #2' (or whichever).")
        lines.append("   To launch with a custom name, say 'launch as <name>'.")
        lines.append("")
        lines.append("   Manual command (default name):")
        lines.append(f"      python3 ~/.claude/claude-optimizer/scripts/session_launcher.py \\")
        lines.append(f"          --intent {handoff_path or '<intent-file>'} \\")
        lines.append(f"          --name '{default_name}'")
    elif level == "info":
        lines.append("")
        lines.append(
            "   You are approaching the configured session budget. Plan a "
            "natural checkpoint soon. Use /budget <N>M to extend if needed."
        )
    elif level == "quality":
        lines.append("")
        lines.append(
            f"   {compactions} compactions have run in this session. Each one "
            "re-compresses already-compressed history (lossiness cascade). "
            "Consider splitting at the next clean boundary."
        )

    return "\n".join(lines)


def main(payload=None):
    cwd = os.getcwd()
    session_uuid, jsonl_path = resolve_session(payload=payload, cwd=cwd)
    if not session_uuid or not jsonl_path:
        return  # no session yet

    state = get_session(session_uuid)
    budget = state.get("budget_tokens", 3_000_000)

    usage = cumulative_session_tokens(jsonl_path)
    tokens_used = usage["total"]  # budget-relevant total (excludes cache reads)
    ratio = tokens_used / budget if budget else 0

    compactions = count_compactions_in_jsonl(jsonl_path)

    # Phase
    phase, conf, breakdown = detect_phase(jsonl_path)
    next_phase = predict_next_phase(phase, breakdown)
    record_phase(session_uuid, phase)

    # Boundary signals (reuse the existing task-hierarchy module)
    boundary_clean = True
    try:
        session_entries_path = get_latest_session_path()
        entries = load_session(session_entries_path) if session_entries_path else []
        signals = extract_signals(entries) if entries else {}
        boundary_clean = _boundary_is_clean(signals)
    except Exception:
        # If signal extraction fails, default to "active" (cautious)
        boundary_clean = False

    # Decide level
    level = None
    if compactions >= COMPACTION_QUALITY_LIMIT:
        level = "quality"
    elif ratio >= SPLIT_THRESHOLD and boundary_clean:
        level = "split"
    elif ratio >= WARN_THRESHOLD and boundary_clean:
        level = "info"
    elif ratio >= SPLIT_THRESHOLD and not boundary_clean:
        level = "info"  # warn but don't push split mid-task

    if level is None:
        return  # silent

    if not can_fire_recommendation(session_uuid):
        return  # respect cooldown / user-declined

    # Generate suggested names + (for split level) write handoff file
    project_name = _project_name_from_cwd(cwd)
    current_session_name = state.get("session_name") or project_name

    # Pull last few user messages for topic extraction
    recent_user_msgs = _extract_recent_user_messages(jsonl_path, n=3)

    enumerated = next_enumerated_name(cwd)
    name_options = generate_session_name(
        phase, next_phase,
        project_name=project_name,
        recent_user_msgs=recent_user_msgs,
        current_session_name=current_session_name,
        enumerated_default=enumerated,
    )
    default_name = name_options["default"]

    handoff_path = None
    if level == "split":
        try:
            handoff_path = write_handoff(
                session_uuid=session_uuid,
                jsonl_path=jsonl_path,
                session_state=state,
                token_usage=usage,
                phase_info={
                    "current": phase, "next": next_phase,
                    "confidence": conf, "breakdown": breakdown,
                },
                new_session_name=default_name,
            )
        except Exception:
            handoff_path = None

    banner = _build_banner(
        level=level,
        tokens_used=tokens_used,
        budget=budget,
        ratio=ratio,
        compactions=compactions,
        phase_info={"current": phase, "next": next_phase, "confidence": conf},
        boundary_clean=boundary_clean,
        name_options=name_options,
        handoff_path=str(handoff_path) if handoff_path else None,
        current_session_name=current_session_name,
    )

    record_recommendation_fired(session_uuid)

    print(json.dumps({"systemMessage": banner}))


if __name__ == "__main__":
    payload = read_hook_payload()
    main(payload=payload)

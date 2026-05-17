#!/usr/bin/env python3
"""cache-bust-warner · PreToolUse hook that asks before cache-busting actions.

When the user/model is about to do something that invalidates the prompt
cache prefix (model switch, CLAUDE.md edit, memory edit), this hook:

  1. Estimates the $ penalty of re-caching the prefix
  2. Reads session signals to judge if NOW is a good moment
  3. Recommends "go ahead" vs "wait until current task wraps"
  4. Asks the user to confirm before proceeding (permissionDecision: ask)

Stdin: tool-call payload from CC's hook protocol.
Stdout: JSON with permissionDecision = "ask" + reason text.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.task_hierarchy import get_latest_session_path, load_session, extract_signals
from lib.cache_bust_state import record_bust
from lib.anatomy import get_model_rates


# ---- Current prefix sizing ----

def get_current_prefix():
    """Return (prefix_tokens, model) from latest session jsonl."""
    cwd = os.getcwd()
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return None, None
    jsonls = sorted(project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonls:
        return None, None
    last_usage = None
    last_model = None
    try:
        with open(jsonls[0]) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message", {})
                if isinstance(msg, dict) and msg.get("usage"):
                    last_usage = msg["usage"]
                    last_model = msg.get("model")
    except Exception:
        return None, None
    if not last_usage:
        return None, None
    prefix = (last_usage.get("cache_read_input_tokens", 0)
              + last_usage.get("cache_creation_input_tokens", 0)
              + last_usage.get("input_tokens", 0))
    return prefix, last_model


def estimate_penalty(prefix_tokens, model):
    """Rebuild cost = full input rate × prefix tokens (since it must be re-cached)."""
    return prefix_tokens / 1e6 * get_model_rates(model)["input"]


# ---- Action classification ----

def classify_action(payload):
    """Return (action_type, reason, details) for cache-busting tool calls.

    action_type in: "model_switch" | "claude_md_edit" | "memory_edit" | None
    """
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    inp = payload.get("tool_input") or payload.get("toolInput") or {}

    if tool == "Bash":
        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
        if "/model" in cmd or cmd.strip().startswith("model "):
            return (
                "model_switch",
                "Model switch — different KV tensors invalidate the cached prefix entirely",
                cmd[:80],
            )

    if tool in ("Edit", "Write"):
        path = ""
        if isinstance(inp, dict):
            path = inp.get("file_path") or inp.get("path", "")
        if "CLAUDE.md" in path:
            return (
                "claude_md_edit",
                f"CLAUDE.md edit — system-prompt cache invalidated",
                path,
            )
        if "/memory/" in path and path.endswith(".md"):
            return (
                "memory_edit",
                f"Memory file edit — may invalidate cache at the memory boundary",
                path,
            )

    return None, "", ""


# ---- Timing recommendation ----

def timing_recommendation(action_type):
    """Use session signals to recommend wait vs go."""
    session_path = get_latest_session_path()
    if not session_path:
        return "ok", "No session signals available."

    entries = load_session(session_path)
    if not entries:
        return "ok", "No signals available."

    s = extract_signals(entries)

    reasons_wait = []
    reasons_go = []

    if s.get("tools_last_3_turns", 0) >= 15:
        reasons_wait.append("rapid tool activity in last 3 turns — looks like active implementation")

    if s.get("recent_tool_errors"):
        reasons_wait.append("recent unresolved tool errors — resolve them before resetting cache")

    if s.get("in_progress_task"):
        reasons_wait.append("a task is still in-progress (TaskList)")

    if s.get("new_file_read_recently"):
        reasons_wait.append("new files read in last 3 turns — those would be cache-invalidated")

    if s.get("tools_this_turn", 0) == 0:
        reasons_go.append("current turn is a discussion turn (no tools) — clean break point")

    if s.get("topic_shift"):
        reasons_go.append("topic just shifted — natural boundary")

    if s.get("velocity_slowing"):
        reasons_go.append("growth slowing — winding down")

    if s.get("idle_min", 0) > 5:
        reasons_go.append(f"idle for {s.get('idle_min'):.0f}+ minutes — between tasks")

    # Action-specific framing
    if action_type == "claude_md_edit":
        action_note = (
            "Top-level instructions change. The new rules will only fully apply "
            "to NEW turns after the cache rebuilds — so the natural moment is "
            "right BEFORE a new task starts."
        )
    elif action_type == "memory_edit":
        action_note = (
            "Memory affects this and future sessions. If you're mid-debugging, "
            "let the fix land first so the memory captures the resolved state."
        )
    elif action_type == "model_switch":
        action_note = (
            "Model swap = totally fresh KV cache. Best done at a clear handoff "
            "(e.g., moving from planning to implementation)."
        )
    else:
        action_note = ""

    # Decide overall recommendation
    if reasons_wait and not reasons_go:
        return "wait", reasons_wait, reasons_go, action_note
    if reasons_go and not reasons_wait:
        return "go", reasons_wait, reasons_go, action_note
    if reasons_wait and reasons_go:
        return "mixed", reasons_wait, reasons_go, action_note
    return "ok", reasons_wait, reasons_go, action_note


# ---- Main ----

def build_message(action_type, reason_short, details, prefix, model, penalty,
                  rec_verdict, reasons_wait, reasons_go, action_note):
    lines = []
    lines.append(f"⚠ CACHE-BUST WARNING ({action_type.replace('_', ' ')})")
    lines.append("")
    lines.append(f"  Action:   {reason_short}")
    if details:
        lines.append(f"  Target:   {details}")
    if prefix:
        lines.append(f"  Prefix:   {prefix:,} tokens currently cached")
        lines.append(f"  Penalty:  ~${penalty:.2f} to rebuild on next API call")
    lines.append("")

    if action_note:
        lines.append(f"  Context:  {action_note}")
        lines.append("")

    # Verdict-based recommendation
    if rec_verdict == "wait":
        lines.append("  💡 RECOMMENDATION: WAIT")
        lines.append("     Current state suggests this is NOT a good moment:")
        for r in reasons_wait:
            lines.append(f"       - {r}")
        lines.append("     Finish current work, then run this action at a clean boundary.")
    elif rec_verdict == "go":
        lines.append("  ✓ RECOMMENDATION: GOOD MOMENT")
        for r in reasons_go:
            lines.append(f"       - {r}")
    elif rec_verdict == "mixed":
        lines.append("  ◐ RECOMMENDATION: MIXED SIGNALS")
        lines.append("     Reasons to WAIT:")
        for r in reasons_wait:
            lines.append(f"       - {r}")
        lines.append("     Reasons it's OK:")
        for r in reasons_go:
            lines.append(f"       - {r}")
    else:
        lines.append("  ℹ Neutral — no strong signal either way.")

    lines.append("")
    lines.append("  Proceed with this action?")
    return "\n".join(lines)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    action_type, reason_short, details = classify_action(payload)
    if not action_type:
        return  # not a cache-busting action

    prefix, model = get_current_prefix()
    penalty = estimate_penalty(prefix, model) if prefix else 0.0

    rec = timing_recommendation(action_type)
    if isinstance(rec, tuple) and len(rec) == 4:
        verdict, reasons_wait, reasons_go, action_note = rec
    else:
        verdict, reasons_wait, reasons_go, action_note = rec[0], [], [], ""

    msg = build_message(
        action_type, reason_short, details, prefix, model, penalty,
        verdict, reasons_wait, reasons_go, action_note,
    )

    # In bypassPermissions mode, CC silences PreToolUse hook reasons AND
    # systemMessage. The only reliable surface left is stderr — which CC
    # surfaces inline in the tool output.
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()

    # Record the bust event so prefix_monitor can track recovery
    session_path = get_latest_session_path()
    if session_path:
        record_bust(
            session_key=str(session_path),
            action_type=action_type,
            target=details,
            pre_bust_prefix=prefix or 0,
            estimated_cost=penalty,
            model=model,
        )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": msg,
        },
        "systemMessage": msg,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()

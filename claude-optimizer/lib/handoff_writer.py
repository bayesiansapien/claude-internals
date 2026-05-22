"""Generate the handoff intent file for a session split.

When the boundary advisor recommends a split and the user confirms, this
module writes a Markdown file inside the CC state directory for the current
project at `~/.claude/projects/<project-hash>/session-handoffs/`. The file
contains:
  - Previous session metadata (UUID, turn count, token usage, compactions)
  - Phase context (current + predicted next)
  - Last N user messages (verbatim, for fast reorientation)
  - Open threads / current task focus (extracted from session activity)
  - Auto-memory pointer

The new session receives this file via `claude --append-system-prompt-file`,
so the assistant in the new session has immediate context for continuation.

Why this location (not /tmp): handoffs are real continuation artifacts, not
throwaway temp files. Putting them with the project's other CC state means
they survive reboots, stay discoverable, and never accidentally leak into
the project's git tree (since ~/.claude/ is outside the repo).
"""

import json
import os
import re
import time
from pathlib import Path


def get_handoff_dir(cwd=None):
    """Return the per-project handoff directory, creating it if needed."""
    cwd = cwd or os.getcwd()
    project_hash = cwd.replace("/", "-")
    d = Path.home() / ".claude" / "projects" / project_hash / "session-handoffs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Legacy constant kept for backward compatibility (not used in new code paths)
HANDOFF_DIR = None  # use get_handoff_dir() instead


def _extract_user_text(msg):
    """Pull the human-readable text out of a user message (skip tool_results)."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        has_tool_result = False
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    has_tool_result = True
                elif block.get("type") == "text":
                    t = block.get("text", "")
                    if isinstance(t, str):
                        texts.append(t)
        if has_tool_result and not texts:
            return None  # pure tool result, not a real user turn
        return " ".join(texts).strip() if texts else None
    return None


def _scan_session(jsonl_path, last_n_user_msgs=5):
    """Walk the session JSONL and extract handoff-relevant data."""
    user_msgs = []
    last_task_state = None
    files_touched_recently = set()
    recent_files = []  # ordered by last-seen
    assistant_msg_count = 0
    last_assistant_text = None

    if not Path(jsonl_path).exists():
        return {
            "user_msgs": [],
            "last_task_state": None,
            "recent_files": [],
            "turn_count": 0,
            "last_assistant_text": None,
        }

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                msg = d.get("message", {})

                if t == "user" and not d.get("isSidechain"):
                    text = _extract_user_text(msg)
                    if text:
                        user_msgs.append(text)

                if t == "assistant":
                    assistant_msg_count += 1
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        last_assistant_text = block.get("text", "")
                                    elif block.get("type") == "tool_use":
                                        name = block.get("name", "")
                                        inp = block.get("input", {})
                                        if name in ("Read", "Edit", "Write", "MultiEdit"):
                                            fp = inp.get("file_path") if isinstance(inp, dict) else None
                                            if fp:
                                                files_touched_recently.add(fp)
                                                if fp not in recent_files:
                                                    recent_files.append(fp)
                                                else:
                                                    recent_files.remove(fp)
                                                    recent_files.append(fp)
                                        elif name == "TaskList":
                                            # Future-state: we could parse the
                                            # tool result for active tasks
                                            pass
    except Exception:
        pass

    return {
        "user_msgs": user_msgs[-last_n_user_msgs:],
        "last_task_state": last_task_state,
        "recent_files": recent_files[-10:],
        "turn_count": assistant_msg_count,
        "last_assistant_text": last_assistant_text,
    }


def _short_summary_from_assistant_text(text, max_chars=240):
    """Pull the first sentence or two from the last assistant turn as a
    one-paragraph reorientation hint."""
    if not text:
        return None
    # Strip code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Take first sentence-ish chunk
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = []
    char_count = 0
    for s in sentences:
        if char_count + len(s) > max_chars:
            break
        out.append(s)
        char_count += len(s)
    return " ".join(out).strip()


def write_handoff(
    session_uuid,
    jsonl_path,
    session_state,
    token_usage,
    phase_info,
    new_session_name,
    out_path=None,
    cwd=None,
):
    """Write the handoff intent file. Returns the path written.

    The file lands in `~/.claude/projects/<project-hash>/session-handoffs/`
    by default. Pass `out_path` to override (for tests).
    """
    ts = int(time.time())
    if out_path is None:
        handoff_dir = get_handoff_dir(cwd)
        short_uuid = session_uuid[:8] if session_uuid else "unknown"
        out_path = handoff_dir / f"intent-{ts}-from-{short_uuid}.md"

    scan = _scan_session(jsonl_path)
    summary = _short_summary_from_assistant_text(scan["last_assistant_text"])

    lines = []
    lines.append(f"# Continuing from prior Claude Code session")
    lines.append("")
    lines.append(f"**New session name:** `{new_session_name}`")
    lines.append("")
    lines.append("## Prior session metadata")
    lines.append(f"- Session UUID: `{session_uuid}`")
    lines.append(f"- Assistant turns: {scan['turn_count']:,}")
    lines.append(f"- Token usage (budget-relevant): {token_usage.get('total', 0):,}")
    lines.append(f"- Cache reads (re-sends): {token_usage.get('cache_reads', 0):,}")
    lines.append(f"- Output tokens: {token_usage.get('output', 0):,}")
    lines.append(f"- Compactions triggered: {session_state.get('compactions', 0)}")
    lines.append("")

    if phase_info:
        lines.append("## Phase context")
        lines.append(f"- Detected current phase: **{phase_info.get('current', 'unknown')}** "
                     f"(confidence {phase_info.get('confidence', 0)})")
        if phase_info.get("next"):
            lines.append(f"- Predicted next phase: **{phase_info['next']}**")
        if phase_info.get("breakdown"):
            lines.append(f"- Recent tool-call distribution: {phase_info['breakdown']}")
        lines.append("")

    if summary:
        lines.append("## What was just happening (assistant's last beat)")
        lines.append(f"> {summary}")
        lines.append("")

    if scan["user_msgs"]:
        lines.append(f"## Last {len(scan['user_msgs'])} user messages (verbatim)")
        for i, msg in enumerate(scan["user_msgs"], 1):
            truncated = msg[:600] + ("..." if len(msg) > 600 else "")
            lines.append(f"{i}. {truncated}")
        lines.append("")

    if scan["recent_files"]:
        lines.append("## Files in flight (recently read/edited)")
        for fp in scan["recent_files"][-10:]:
            lines.append(f"- `{fp}`")
        lines.append("")

    lines.append("## Auto-memory")
    lines.append(
        "Persistent project memory (Tier 1) lives at "
        "`~/.claude/projects/<this-project-hash>/memory/` and is auto-loaded "
        "at session start. Cross-session facts (user profile, project goals, "
        "feedback rules, references) carry over without re-explanation."
    )
    lines.append("")

    lines.append("## Continue from")
    lines.append(
        "Pick up the work indicated by the last user message above. The phase "
        "context tells you what kind of work was active. If the user gives "
        "you a fresh prompt, defer to that — this handoff is a reorientation, "
        "not a constraint."
    )

    content = "\n".join(lines) + "\n"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content)
    except Exception as e:
        return None
    return out_path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.session_budget_state import (
        find_session_jsonl_for_cwd, get_session, cumulative_session_tokens
    )
    from lib.phase_detector import detect_phase, predict_next_phase, generate_session_name

    sid, jsonl = find_session_jsonl_for_cwd()
    if not jsonl:
        print("No session found.")
        sys.exit(0)
    state = get_session(sid)
    usage = cumulative_session_tokens(jsonl)
    phase, conf, breakdown = detect_phase(jsonl)
    next_phase = predict_next_phase(phase, breakdown)
    name_opts = generate_session_name(
        phase, next_phase, project_name="mine-cc",
        current_session_name=state.get("session_name"),
    )
    out = write_handoff(
        session_uuid=sid,
        jsonl_path=jsonl,
        session_state=state,
        token_usage=usage,
        phase_info={"current": phase, "next": next_phase, "confidence": conf, "breakdown": breakdown},
        new_session_name=name_opts["default"],
    )
    print(f"Handoff written to: {out}")
    print("=" * 60)
    if out:
        print(out.read_text()[:1500])

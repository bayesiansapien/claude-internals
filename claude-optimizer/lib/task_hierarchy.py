"""Extracts hierarchical task signals from session jsonl.

Project → Macro task → Local task → Sub-local activity.
All inferred from the session transcript; no I/O beyond reading jsonl.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.anatomy import get_context_window


STOP_WORDS = set(
    "a an the is are was were be been being do does did doing have has had "
    "having i you we they it this that these those of in on at for with to "
    "from by as and or but if so my our your me us them him her his hers".split()
)


def get_latest_session_path():
    cwd = os.getcwd()
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return None
    jsonls = sorted(project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonls[0] if jsonls else None


def load_session(jsonl_path, max_lines=10000):
    entries = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    # Keep tail to bound memory
    if len(entries) > max_lines:
        entries = entries[-max_lines:]
    return entries


def tokenize(text):
    if not isinstance(text, str):
        return []
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def _parse_ts(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_real_user_msg(entry):
    """True if this is a genuine user text input (not a tool_result)."""
    if entry.get("type") != "user":
        return False
    if entry.get("isSidechain"):
        return False
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return not entry.get("toolUseResult")
    if isinstance(content, list):
        # Must contain at least one text block AND no tool_result blocks
        has_text = any(isinstance(c, dict) and c.get("type") == "text" for c in content)
        has_tool_result = any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        )
        return has_text and not has_tool_result
    return False


def _extract_user_text(entry):
    msg = entry.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts)
    return ""


def extract_signals(entries):
    """Compute all signals needed for scoring + tier 2/3 judgement."""
    signals = {
        "total_turns": 0,
        "tools_this_turn": 0,
        "tools_last_3_turns": 0,
        "idle_min": 0,
        "topic_shift": False,
        "topic_overlap": None,
        "velocity_slowing": False,
        "new_skill_loaded_recently": False,
        "new_file_read_recently": False,
        "recent_tool_errors": False,
        "in_progress_task": False,
        "user_prompts": [],
        "macro_keywords": [],
        "prefix_tokens": 0,
        "pressure_pct": 0,
        "model": None,
        "window": 200_000,
    }
    if not entries:
        return signals

    # Filter to main thread (skip subagent sidechains)
    main = [e for e in entries if not e.get("isSidechain", False)]

    # Walk and split into turns (user → assistant cluster)
    turns = []
    current = None
    user_prompts = []
    last_user_ts_str = None
    last_assistant_ts_str = None

    for e in main:
        if _is_real_user_msg(e):
            text = _extract_user_text(e)
            ts = e.get("timestamp")
            user_prompts.append({"ts": ts, "text": text})
            last_user_ts_str = ts
            if current is not None:
                turns.append(current)
            current = {
                "tool_uses": [],
                "files_read": [],
                "skills_loaded": [],
                "had_errors": False,
                "input_tokens": 0,
            }
        elif e.get("type") == "assistant" and current is not None:
            last_assistant_ts_str = e.get("timestamp")
            msg = e.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", [])
            usage = msg.get("usage")
            if usage:
                current["input_tokens"] = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        current["tool_uses"].append(c.get("name"))
                        inp = c.get("input", {}) or {}
                        if c.get("name") == "Read" and isinstance(inp, dict):
                            fp = inp.get("file_path")
                            if fp:
                                current["files_read"].append(fp)
                        if c.get("name") == "Skill" and isinstance(inp, dict):
                            sk = inp.get("skill")
                            if sk:
                                current["skills_loaded"].append(sk)
        elif e.get("type") == "user" and current is not None:
            # Could be a tool_result with is_error
            msg = e.get("message", {})
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for c in content:
                    if (isinstance(c, dict) and c.get("type") == "tool_result"
                            and c.get("is_error")):
                        current["had_errors"] = True

    if current is not None:
        turns.append(current)

    signals["total_turns"] = len(turns)
    signals["user_prompts"] = user_prompts

    if turns:
        signals["tools_this_turn"] = len(turns[-1]["tool_uses"])
        last_3 = turns[-3:]
        signals["tools_last_3_turns"] = sum(len(t["tool_uses"]) for t in last_3)
        signals["recent_tool_errors"] = any(t["had_errors"] for t in turns[-3:])

    # Idle time: now - last user message timestamp
    last_user_ts = _parse_ts(last_user_ts_str)
    if last_user_ts:
        now = datetime.now(timezone.utc)
        signals["idle_min"] = max(0, (now - last_user_ts).total_seconds() / 60)

    # Topic shift: compare current prompt vs UNION of last 5 prompts.
    # Comparing only to immediately-previous prompt produces too many false
    # positives (short prompts naturally have low overlap). Last-5 union is
    # a more stable "what have we been talking about lately" signal.
    if len(user_prompts) >= 2:
        curr = set(tokenize(user_prompts[-1]["text"]))
        recent_union = set()
        for p in user_prompts[-6:-1]:  # last 5 prior to current
            recent_union.update(tokenize(p["text"]))
        if curr and recent_union:
            overlap = len(curr & recent_union) / max(len(curr), 1)
            signals["topic_overlap"] = overlap
            signals["topic_shift"] = overlap < 0.2  # tighter threshold
            signals["topic_shift_evidence"] = {
                "current_keywords": sorted(curr)[:15],
                "recent_keywords_sample": sorted(recent_union)[:20],
                "overlap_pct": round(overlap * 100, 1),
                "compared_against": f"last {min(5, len(user_prompts)-1)} user prompts",
            }

    # Macro keywords: union of tokens in first 3 prompts
    if user_prompts:
        macro = set()
        for p in user_prompts[:3]:
            macro.update(tokenize(p["text"]))
        signals["macro_keywords"] = sorted(macro)[:40]

    # Velocity: input_tokens growth slowing across recent turns
    if len(turns) >= 4:
        recent = [t["input_tokens"] for t in turns[-4:] if t["input_tokens"]]
        if len(recent) >= 3:
            deltas = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
            if deltas[-1] < deltas[0] * 0.6:  # last delta is < 60% of earliest
                signals["velocity_slowing"] = True

    # Info-loss safeguards (looking at last 5 turns)
    last_5 = turns[-5:] if turns else []
    signals["new_skill_loaded_recently"] = any(t["skills_loaded"] for t in last_5)

    earlier_reads = set()
    for t in turns[:-3]:
        earlier_reads.update(t.get("files_read", []))
    recent_reads = set()
    for t in turns[-3:]:
        recent_reads.update(t.get("files_read", []))
    signals["new_file_read_recently"] = bool(recent_reads - earlier_reads)

    # Prefix size from latest assistant usage
    last_usage = None
    last_model = None
    for e in main:
        if e.get("type") == "assistant":
            msg = e.get("message", {})
            if isinstance(msg, dict) and msg.get("usage"):
                last_usage = msg["usage"]
                last_model = msg.get("model")
    if last_usage:
        prefix = (
            last_usage.get("cache_read_input_tokens", 0)
            + last_usage.get("cache_creation_input_tokens", 0)
            + last_usage.get("input_tokens", 0)
        )
        signals["prefix_tokens"] = prefix
        signals["model"] = last_model
        signals["window"] = get_context_window(last_model)
        signals["pressure_pct"] = prefix / signals["window"] * 100

    return signals

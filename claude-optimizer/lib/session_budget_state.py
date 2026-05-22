"""Per-session budget + compaction tracking.

State file: ~/.claude/session-budget-state.json
Schema:
  {
    "<session_uuid>": {
      "budget_tokens": 3000000,
      "set_at_ts": 1747934800.0,
      "compactions": 0,
      "last_phase": "research",
      "phase_history": [
        {"ts": 1747934800.0, "phase": "research"}
      ],
      "boundary_recommendations_fired": 0,
      "last_recommendation_ts": null,
      "user_declined_launch": false
    }
  }

The session_uuid is the bare UUID (without .jsonl) that names the session
transcript inside ~/.claude/projects/<project-hash>/<uuid>.jsonl.
"""

import json
import os
import time
from pathlib import Path

STATE_PATH = Path.home() / ".claude" / "session-budget-state.json"
DEFAULT_BUDGET = 3_000_000  # 3M tokens

# Tokens-per-turn ceiling to prevent the advisor from firing on artificial spikes
RECOMMENDATION_COOLDOWN_SECONDS = 600  # 10 minutes between repeat banners


def _load():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def init_session(session_uuid, budget_tokens=None):
    """Initialize state for a new session. Idempotent."""
    state = _load()
    if session_uuid in state:
        return state[session_uuid]
    budget = budget_tokens or int(
        os.environ.get("CC_SESSION_TOKEN_LIMIT", str(DEFAULT_BUDGET))
    )
    state[session_uuid] = {
        "budget_tokens": budget,
        "set_at_ts": time.time(),
        "compactions": 0,
        "last_phase": None,
        "phase_history": [],
        "boundary_recommendations_fired": 0,
        "last_recommendation_ts": None,
        "user_declined_launch": False,
    }
    _save(state)
    return state[session_uuid]


def get_session(session_uuid):
    """Read state for a session. Auto-initializes if missing."""
    state = _load()
    if session_uuid not in state:
        return init_session(session_uuid)
    return state[session_uuid]


def update_session(session_uuid, updates):
    state = _load()
    if session_uuid not in state:
        state[session_uuid] = init_session(session_uuid)
    state[session_uuid].update(updates)
    _save(state)
    return state[session_uuid]


def set_budget(session_uuid, budget_tokens):
    """Override the budget for an existing session."""
    return update_session(session_uuid, {
        "budget_tokens": int(budget_tokens),
        "set_at_ts": time.time(),
    })


def increment_compaction(session_uuid):
    s = get_session(session_uuid)
    return update_session(session_uuid, {
        "compactions": s.get("compactions", 0) + 1,
    })


def record_phase(session_uuid, phase):
    """Append a phase observation. Caps history at 50 entries."""
    s = get_session(session_uuid)
    history = s.get("phase_history", [])
    if history and history[-1].get("phase") == phase:
        # Same phase as last observation, no new entry
        return s
    history.append({"ts": time.time(), "phase": phase})
    return update_session(session_uuid, {
        "last_phase": phase,
        "phase_history": history[-50:],
    })


def record_recommendation_fired(session_uuid):
    s = get_session(session_uuid)
    return update_session(session_uuid, {
        "boundary_recommendations_fired": s.get("boundary_recommendations_fired", 0) + 1,
        "last_recommendation_ts": time.time(),
    })


def record_user_declined(session_uuid):
    return update_session(session_uuid, {"user_declined_launch": True})


def can_fire_recommendation(session_uuid):
    """Anti-spam: only fire once per cooldown window, and once after user declines."""
    s = get_session(session_uuid)
    if s.get("user_declined_launch"):
        # User said no this session. Stay silent for the rest.
        return False
    last = s.get("last_recommendation_ts")
    if last is None:
        return True
    return (time.time() - last) > RECOMMENDATION_COOLDOWN_SECONDS


def count_compactions_in_jsonl(jsonl_path):
    """Walk a session JSONL and count compaction events.

    Claude Code records compaction summaries as messages with a specific
    type marker. We look for any of:
      - top-level "type" == "summary" or "compactSummary"
      - message.role == "user" and message.content contains "Previous Conversation Compacted"
        (this is the system-injected compaction marker)
    """
    if not Path(jsonl_path).exists():
        return 0
    count = 0
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                if t in ("summary", "compactSummary", "compact_summary"):
                    count += 1
                    continue
                msg = d.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and "Previous Conversation Compacted" in content:
                        count += 1
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict):
                                text = c.get("text", "")
                                if isinstance(text, str) and "Previous Conversation Compacted" in text:
                                    count += 1
                                    break
    except Exception:
        return 0
    return count


def cumulative_session_tokens(jsonl_path):
    """Return token counts for budgeting purposes.

    The 'total' field is the budget-relevant figure: fresh input (cache writes
    + uncached input) + output. This excludes cache_read_input_tokens because
    those are deterministic re-reads of bytes the model has already processed
    — they cost real money but are not "new work." A 3M-token budget on this
    metric tracks ~4 hours of typical Opus coding work.

    'total_with_cache_reads' is the gross number including re-reads. Useful
    for users who want to budget against raw API throughput regardless of
    caching.
    """
    if not Path(jsonl_path).exists():
        return {
            "fresh_input": 0, "cache_reads": 0, "output": 0,
            "total": 0, "total_with_cache_reads": 0,
        }
    fresh_input = 0
    cache_reads = 0
    total_output = 0
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message", {})
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                fresh_input += (
                    u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                )
                cache_reads += u.get("cache_read_input_tokens", 0)
                total_output += u.get("output_tokens", 0)
    except Exception:
        return {
            "fresh_input": 0, "cache_reads": 0, "output": 0,
            "total": 0, "total_with_cache_reads": 0,
        }
    total = fresh_input + total_output
    return {
        "fresh_input": fresh_input,
        "cache_reads": cache_reads,
        "output": total_output,
        "total": total,
        "total_with_cache_reads": total + cache_reads,
    }


def find_session_jsonl_for_cwd(cwd=None):
    """Return (session_uuid, jsonl_path) for the most recent session in this project."""
    cwd = cwd or os.getcwd()
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return None, None
    jsonls = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonls:
        return None, None
    path = jsonls[0]
    return path.stem, path


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        sid, jsonl = find_session_jsonl_for_cwd()
        if not sid:
            print("No session found for this project.")
            sys.exit(0)
        print(f"Session UUID: {sid}")
        print(f"State: {json.dumps(get_session(sid), indent=2)}")
        print(f"Token usage: {cumulative_session_tokens(jsonl)}")
        print(f"Compactions in JSONL: {count_compactions_in_jsonl(jsonl)}")
    else:
        print("Usage: python3 session_budget_state.py show")

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
      "user_declined_launch": false,
      "session_name": "cc-internals-2",
      "project_cwd": "/Users/.../mine-cc"
    }
  }

The session_uuid is the bare UUID (without .jsonl) that names the session
transcript inside ~/.claude/projects/<project-hash>/<uuid>.jsonl.

session_name is a human-readable label for the session. It defaults to the
project's directory name (e.g. "mine-cc") for the first session, and is
auto-enumerated for subsequent sessions in the same project (cc-internals-2,
cc-internals-3, ...). The handoff flow lets the user override with a
phase-based suggested name at split time.
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


def init_session(session_uuid, budget_tokens=None, session_name=None, project_cwd=None):
    """Initialize state for a new session. Idempotent.

    If session_name is omitted, resolution order:
      1. CC_SESSION_TITLE env var (set by the launcher for handoff-spawned sessions)
      2. pending-name.txt file inside the project's handoff dir
      3. Auto-enumerated default from project's prior session names
    """
    state = _load()
    if session_uuid in state:
        return state[session_uuid]
    budget = budget_tokens or int(
        os.environ.get("CC_SESSION_TOKEN_LIMIT", str(DEFAULT_BUDGET))
    )
    cwd = project_cwd or os.getcwd()
    if session_name is None:
        session_name = _resolve_initial_session_name(cwd, state)
    state[session_uuid] = {
        "budget_tokens": budget,
        "set_at_ts": time.time(),
        "compactions": 0,
        "last_phase": None,
        "phase_history": [],
        "boundary_recommendations_fired": 0,
        "last_recommendation_ts": None,
        "user_declined_launch": False,
        "session_name": session_name,
        "project_cwd": cwd,
    }
    _save(state)
    return state[session_uuid]


def _resolve_initial_session_name(cwd, state):
    """Determine the name for a fresh session.

    Order:
      1. CC_SESSION_TITLE env var (handoff-spawned)
      2. pending-name.txt in the project's session-handoffs dir (handoff-spawned)
      3. next_enumerated_name(cwd, state) — auto-increments based on prior names
    """
    env_name = os.environ.get("CC_SESSION_TITLE")
    if env_name:
        return env_name.strip()

    pending = Path(cwd).expanduser() if False else None
    # pending-name.txt lives at ~/.claude/projects/<hash>/session-handoffs/pending-name.txt
    handoff_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-") / "session-handoffs"
    pending_file = handoff_dir / "pending-name.txt"
    if pending_file.exists():
        try:
            name = pending_file.read_text().strip()
            # Consume it — single-use
            pending_file.unlink()
            if name:
                return name
        except Exception:
            pass

    return next_enumerated_name(cwd, state)


def project_session_names(cwd, state=None):
    """Return list of session_names already used in the given project (cwd-matched)."""
    state = state if state is not None else _load()
    names = []
    for uuid, s in state.items():
        if s.get("project_cwd") == cwd and s.get("session_name"):
            names.append(s["session_name"])
    return names


def next_enumerated_name(cwd, state=None):
    """Compute the next enumerated session name for the project.

    First session in the project gets the bare directory name (e.g. "mine-cc").
    Subsequent sessions get "<base>-2", "<base>-3", ... using whichever base
    is most common among prior sessions.
    """
    import re
    project_base = Path(cwd).name or "session"
    state = state if state is not None else _load()
    prior_names = project_session_names(cwd, state)

    if not prior_names:
        return project_base

    # Discover the base used by prior sessions and the highest enumeration seen
    base_counts = {}
    max_n = 1
    for name in prior_names:
        m = re.match(r"^(.+?)(?:-(\d+))?$", name)
        if not m:
            continue
        base = m.group(1)
        n = int(m.group(2)) if m.group(2) else 1
        base_counts[base] = base_counts.get(base, 0) + 1
        if n > max_n:
            max_n = n

    if base_counts:
        # Pick the most common base (ties broken alphabetically for determinism)
        base = sorted(base_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    else:
        base = project_base
    return f"{base}-{max_n + 1}"


def set_session_name(session_uuid, name):
    """Update a session's human-readable name."""
    return update_session(session_uuid, {"session_name": name})


def get_session_name(session_uuid):
    s = get_session(session_uuid)
    return s.get("session_name")


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

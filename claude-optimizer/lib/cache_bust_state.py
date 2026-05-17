"""Per-session cache-bust event tracking.

Records bust events so prefix_monitor can show recovery progress: "how much
of the $X rebuild has been earned back through cache-read savings on the new
model since the bust?". Each post-bust turn's cache_reads contribute
cache_reads × (input_rate − cache_read_rate) toward recovery.

State file: ~/.claude/cache-bust-events.json
Schema:
  { "<session_path>": [ { event_dict, ... }, ... ] }

Field names retain `post_bust_accumulated_cost` / `_turn_count` for backward
compatibility, but they now hold cumulative recovered $ and user-turn count.
"""

import json
import time
from pathlib import Path


STATE_PATH = Path.home() / ".claude" / "cache-bust-events.json"

PHANTOM_THRESHOLD = 0.02  # USD — below this an "actual_rebuild_cost" is noise
PHANTOM_TTL_SEC = 3600    # phantoms older than 1h get GC'd


def _gc(state):
    """Drop test-session keys and stale phantom/orphan events. Sort each
    session's events by ts so reversed() walks chronological-newest first."""
    now = time.time()
    cleaned = {}
    for sk, events in state.items():
        if sk.startswith("/tmp/") or "/tmp/" in sk[:10]:
            continue
        # First pass: sort by ts so we know which event is the latest
        events_sorted = sorted(events, key=lambda e: e.get("ts") or 0)
        latest_ts = events_sorted[-1].get("ts") or 0 if events_sorted else 0

        kept = []
        for ev in events_sorted:
            actual = ev.get("actual_rebuild_cost") or 0
            recovered = ev.get("post_bust_accumulated_cost") or 0
            reached = ev.get("break_even_reached")
            age = now - (ev.get("ts") or 0)
            is_latest = (ev.get("ts") or 0) == latest_ts

            # Drop phantoms older than TTL
            if actual < PHANTOM_THRESHOLD and age > PHANTOM_TTL_SEC:
                continue
            # Drop orphaned busts: superseded by a newer bust before recovery
            # could even start accumulating ($0 recovered, not resolved, not
            # the most-recent). They pollute history but can never resolve.
            if (not is_latest and not reached and recovered == 0
                    and age > PHANTOM_TTL_SEC):
                continue
            # Clamp over-accumulated recovered values (legacy data from before
            # the cap was added).
            if actual > 0 and recovered > actual:
                ev["post_bust_accumulated_cost"] = round(actual, 4)
            kept.append(ev)
        if kept:
            cleaned[sk] = kept[-20:]
    return cleaned


def _load():
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text())
        return _gc(raw)
    except Exception:
        return {}


def _save(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(_gc(state), indent=2))
    except Exception:
        pass


def record_bust(session_key, action_type, target, pre_bust_prefix,
                estimated_cost, model, ts=None):
    if session_key.startswith("/tmp/"):
        # Never write test-session pollution to the real state file
        return
    state = _load()
    events = state.setdefault(session_key, [])
    events.append({
        "ts": ts if ts is not None else time.time(),
        "action_type": action_type,
        "target": target,
        "pre_bust_prefix": pre_bust_prefix,
        "estimated_rebuild_cost": estimated_cost,
        "actual_rebuild_cost": None,
        "model": model,
        "post_bust_accumulated_cost": 0.0,
        "post_bust_turn_count": 0,
        "break_even_reached": False,
        "break_even_at_turn": None,
    })
    events.sort(key=lambda e: e.get("ts") or 0)
    state[session_key] = events[-20:]
    _save(state)


def latest_open_bust(session_key):
    """Most recent unresolved bust event, or None.

    'Open' = either actual_rebuild_cost not yet set OR break-even not reached.
    """
    state = _load()
    events = state.get(session_key, [])
    for ev in reversed(events):
        if not ev.get("break_even_reached"):
            return ev
    return None


def latest_confirmed_bust(session_key):
    """Most recent bust with a real confirmed rebuild cost that hasn't been resolved.

    Filters out:
      - Fake/test events (actual_rebuild_cost < $0.02)
      - Already-resolved busts (break_even_reached = True) → go silent

    Returns None when the most recent real bust already hit break-even,
    so callers show nothing. A new bust after that will start fresh.
    """
    state = _load()
    events = state.get(session_key, [])
    for ev in reversed(events):
        actual = ev.get("actual_rebuild_cost") or 0
        if actual >= 0.02:
            # Most recent real bust — if resolved, go silent
            if ev.get("break_even_reached"):
                return None
            return ev
    return None


def update_latest_bust(session_key, updates, target_ts=None):
    """Update an event. If target_ts is given, find by ts (auto-detector path).
    Otherwise update the latest confirmed bust (actual>=threshold) to stay
    consistent with latest_confirmed_bust's selection."""
    state = _load()
    events = state.get(session_key, [])
    if not events:
        return None
    target = None
    if target_ts is not None:
        for ev in events:
            if abs((ev.get("ts") or 0) - target_ts) < 1.0:
                target = ev
                break
    else:
        for ev in reversed(events):
            actual = ev.get("actual_rebuild_cost") or 0
            if actual >= PHANTOM_THRESHOLD:
                target = ev
                break
        if target is None:
            target = events[-1]
    if target is None:
        return None
    target.update(updates)
    state[session_key] = events
    _save(state)
    return target


def all_events_for_session(session_key):
    state = _load()
    return state.get(session_key, [])

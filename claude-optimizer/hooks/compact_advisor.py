#!/usr/bin/env python3
"""compact-advisor · Stop hook that recommends /compact at task boundaries.

Uses the unified compact_decision scorer — same logic as /compact-suggest.
Adds:
  - Anti-spam state (per-session)
  - Banner output via systemMessage (visible in CC UI)
  - Honest gating (only fires when verdict is actionable AND spam window allows)

Cost budget per long session: $0–$0.20 worst case (T2 Haiku ~$0.001/call,
T3 Sonnet ~$0.05/call, fires rarely).
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.compact_decision import decide_compact


STATE_PATH = Path.home() / ".claude" / "compact-advisor-state.json"
CONFIG_PATH = Path.home() / ".claude" / "prompt-rewriter-config.json"

DEFAULTS = {
    "enabled": True,
    "anti_spam_turns": 5,
    "run_t2_haiku": True,
    "run_t3_sonnet": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            full = json.loads(CONFIG_PATH.read_text())
            cfg.update(full.get("compact_advisor", {}))
        except Exception:
            pass
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def banner_for_verdict(verdict, reason, tiers_run, pressure_pct, projected_savings):
    tiers_tag = "+".join(tiers_run) if tiers_run else "T1"
    pct = f"{pressure_pct:.0f}%"
    save_str = ""
    if projected_savings is not None and projected_savings > 5:
        save_str = f" · ~${projected_savings:.0f} savings"

    if verdict == "COMPACT_NOW":
        return f"🚨 Compact advisor [{tiers_tag}]: {reason} ({pct} used){save_str}"
    if verdict == "SOON":
        return f"⚠ Compact advisor [{tiers_tag}]: {reason} ({pct} used){save_str}"
    return None  # WAIT or NO_ACTION → no banner


def main():
    # Drain stdin (we don't need the payload)
    try:
        sys.stdin.read()
    except Exception:
        pass

    cfg = load_config()
    if not cfg.get("enabled"):
        return

    d = decide_compact(
        run_t2=cfg.get("run_t2_haiku", True),
        run_t3=cfg.get("run_t3_sonnet", True),
    )

    if d.get("below_pressure_gate"):
        return

    verdict = d["verdict"]
    if verdict not in ("COMPACT_NOW", "SOON"):
        # Includes WAIT (T3 veto) and NO_ACTION
        return

    # Anti-spam — per session
    state = load_state()
    session_key = str(Path.home() / ".claude" / "projects")  # placeholder; refined below
    # Use the actual session file path as the key
    from lib.task_hierarchy import get_latest_session_path
    sp = get_latest_session_path()
    if sp:
        session_key = str(sp)

    sess_state = state.get(session_key, {})
    total_turns = (d["signals"] or {}).get("total_turns", 0)
    last_fired = sess_state.get("last_fired_turn", 0)
    if total_turns - last_fired < cfg["anti_spam_turns"]:
        return

    projection = d.get("projection") or {}
    projected_savings = projection.get("projected_savings")
    anatomy = d.get("anatomy") or {}
    pressure_pct = anatomy.get("pressure_pct", 0)

    banner = banner_for_verdict(
        verdict, d["verdict_reason"], d["tiers_run"],
        pressure_pct, projected_savings,
    )
    if not banner:
        return

    sess_state["last_fired_turn"] = total_turns
    history = sess_state.setdefault("fired_history", [])
    history.append({
        "turn": total_turns,
        "verdict": verdict,
        "score": (d.get("boundary") or {}).get("score"),
        "pressure_pct": round(pressure_pct, 1),
        "projected_savings": projected_savings,
        "tiers": d["tiers_run"],
        "ts": time.time(),
    })
    sess_state["fired_history"] = history[-20:]
    state[session_key] = sess_state
    save_state(state)

    print(json.dumps({"systemMessage": banner}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)

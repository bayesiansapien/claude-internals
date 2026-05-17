"""Unified compaction decision — single source of truth.

Combines:
  - Anatomy: where every prefix token lives
  - Projection: growth rate, headroom, economic savings
  - Boundary score: deterministic T1 signals (task hierarchy, info-loss)
  - T2 Haiku tiebreaker: when T1 is ambiguous (4-6)
  - T3 Sonnet judge: when pressure ≥85% and ambiguous

Both /compact-suggest (skill) and compact_advisor (hook) call this function.
The hook adds anti-spam state and renders the verdict as a banner; the skill
shows the full breakdown.
"""

from .anatomy import (
    compute_anatomy,
    compute_projection,
    get_oauth_token,
)
from .task_hierarchy import (
    get_latest_session_path,
    load_session,
    extract_signals,
)
from .judges.haiku_judge import ask_haiku
from .judges.main_model_judge import ask_main_model


# ---- Boundary scoring (folded in from old boundary_score.py) ----

def _score_boundary(signals, pressure_pct):
    """Deterministic T1 score from session signals. Returns (score, breakdown,
    info_loss_flags)."""
    score = 0
    breakdown = {}
    info_loss_flags = []

    if pressure_pct < 50:
        p = 0
    elif pressure_pct < 70:
        p = 1
    elif pressure_pct < 85:
        p = 3
    elif pressure_pct < 95:
        p = 6
    else:
        p = 10
    score += p
    breakdown["pressure"] = p

    if signals.get("tools_this_turn", 0) == 0:
        score += 2
        breakdown["discussion_turn"] = 2

    tools_3 = signals.get("tools_last_3_turns", 0)
    if tools_3 < 5:
        score += 1
        breakdown["winding_down"] = 1

    if signals.get("idle_min", 0) > 5:
        score += 2
        breakdown["idle_pause"] = 2

    if signals.get("topic_shift", False):
        score += 3
        breakdown["topic_shift"] = 3

    if signals.get("velocity_slowing", False):
        score += 1
        breakdown["velocity_slowing"] = 1

    if tools_3 > 15:
        score -= 3
        breakdown["rapid_work"] = -3

    if signals.get("in_progress_task", False):
        score -= 2
        breakdown["active_task"] = -2

    if signals.get("new_skill_loaded_recently", False):
        score -= 2
        breakdown["new_skill"] = -2
        info_loss_flags.append("new skill loaded in last 5 turns")

    if signals.get("recent_tool_errors", False):
        score -= 2
        breakdown["error_recovery"] = -2
        info_loss_flags.append("unresolved tool errors in last 3 turns")

    if signals.get("new_file_read_recently", False):
        score -= 1
        breakdown["new_context"] = -1
        info_loss_flags.append("new files read in last 3 turns")

    return score, breakdown, info_loss_flags


# ---- Verdict synthesis ----

def _synthesize_verdict(pressure_pct, score, projected_savings, t3_verdict, t3_reason):
    """Combine all signals into a final verdict + reason."""
    if t3_verdict == "WAIT":
        return "WAIT", f"Sonnet judge: {t3_reason or 'wait'}"
    if t3_verdict == "COMPACT_NOW":
        return "COMPACT_NOW", f"Sonnet judge: {t3_reason or 'compact now'}"

    if pressure_pct >= 95:
        return "COMPACT_NOW", f"Critical pressure ({pressure_pct:.0f}%)"
    if pressure_pct >= 85 and score >= 6:
        return "COMPACT_NOW", f"High pressure ({pressure_pct:.0f}%) + boundary"
    if pressure_pct >= 70 and score >= 6:
        return "SOON", f"Approaching limits ({pressure_pct:.0f}%) + boundary"
    if score >= 9:
        return "SOON", "Strong boundary detected"
    if score >= 7 and projected_savings is not None and projected_savings > 5:
        return "SOON", f"Boundary + economic gain (~${projected_savings:.0f})"
    if projected_savings is not None and projected_savings > 10 and score >= 5:
        return "SOON", f"Economic gain (~${projected_savings:.0f})"
    return "NO_ACTION", "No urgency; no boundary"


# ---- Main entrypoint ----

def decide_compact(run_t2=True, run_t3=True, min_warmup_turns=5):
    """Returns the full decision dict — used by skill AND hook.

    Schema:
      {
        "below_pressure_gate": bool,         # True = no further analysis
        "anatomy": {...},                    # from compute_anatomy
        "projection": {...},                 # from compute_projection
        "signals": {...},                    # from task_hierarchy.extract_signals
        "boundary": {
          "score": int,
          "breakdown": {...},
          "info_loss_flags": [str],
        },
        "tiers_run": ["T1", "T2:..", "T3:.."],
        "t2_verdict": True|False|None,       # True = boundary, False = same, None = skipped
        "t3_verdict": "COMPACT_NOW"|"SOON"|"WAIT"|None,
        "t3_reason": str|None,
        "verdict": "COMPACT_NOW"|"SOON"|"WAIT"|"NO_ACTION",
        "verdict_reason": str,
      }
    """
    token = get_oauth_token()

    session_path = get_latest_session_path()
    entries = load_session(session_path) if session_path else []
    signals = extract_signals(entries) if entries else {}

    anatomy = compute_anatomy(token=token)
    pressure_pct = anatomy["pressure_pct"]
    total = anatomy["total_prefix"]
    window = anatomy["window"]

    out = {
        "anatomy": anatomy,
        "signals": signals,
        "tiers_run": [],
        "t2_verdict": None,
        "t3_verdict": None,
        "t3_reason": None,
    }

    # Pressure gate
    if pressure_pct < 50 or signals.get("total_turns", 0) < min_warmup_turns:
        out["below_pressure_gate"] = True
        out["projection"] = compute_projection(window, total)
        out["boundary"] = {"score": 0, "breakdown": {}, "info_loss_flags": []}
        out["verdict"] = "NO_ACTION"
        out["verdict_reason"] = "Below pressure gate (50%) or session too new"
        return out

    out["below_pressure_gate"] = False

    # Projection (free)
    projection = compute_projection(window, total)
    out["projection"] = projection
    projected_savings = projection.get("projected_savings") if projection else None

    # T1: boundary score
    score, breakdown, info_loss = _score_boundary(signals, pressure_pct)
    out["boundary"] = {
        "score": score,
        "breakdown": breakdown,
        "info_loss_flags": info_loss,
    }
    out["tiers_run"].append("T1")

    # T2: Haiku tiebreaker when ambiguous
    if run_t2 and 4 <= score <= 6:
        prompts = signals.get("user_prompts") or []
        if len(prompts) >= 2:
            boundary = ask_haiku(prompts[-2].get("text", ""),
                                 prompts[-1].get("text", ""))
            out["t2_verdict"] = boundary
            if boundary is True:
                score += 2
                breakdown["haiku_boundary"] = 2
                out["tiers_run"].append("T2:boundary")
            elif boundary is False:
                # Same topic — strong "not a boundary" signal
                score -= 2
                breakdown["haiku_same_topic"] = -2
                out["tiers_run"].append("T2:same")
            out["boundary"]["score"] = score
            out["boundary"]["breakdown"] = breakdown

    # T3: Sonnet judge for high-stakes ambiguity
    if (run_t3
            and pressure_pct >= 85
            and 4 <= score <= 8):
        payload = {
            "pressure_pct": pressure_pct,
            "window": window,
            "macro_keywords": signals.get("macro_keywords", []),
            "recent_user_prompts": (signals.get("user_prompts") or [])[-5:],
            "tools_this_turn": signals.get("tools_this_turn", 0),
            "tools_last_3_turns": signals.get("tools_last_3_turns", 0),
            "new_file_read_recently": signals.get("new_file_read_recently", False),
            "new_skill_loaded_recently": signals.get("new_skill_loaded_recently", False),
        }
        result = ask_main_model(payload)
        if result:
            out["t3_verdict"] = result.get("verdict")
            out["t3_reason"] = result.get("reason", "")
            out["tiers_run"].append(f"T3:{out['t3_verdict']}")

    verdict, reason = _synthesize_verdict(
        pressure_pct, score, projected_savings,
        out["t3_verdict"], out["t3_reason"],
    )
    out["verdict"] = verdict
    out["verdict_reason"] = reason

    return out

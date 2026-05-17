#!/usr/bin/env python3
"""prefix-monitor · Stop hook — emits prefix-size status line + cache-bust
recovery tracker if a recent bust happened.

Recovery framing: each turn after a bust, cache_reads earn back
cache_reads × (input_rate − cache_read_rate) on the NEW model.
Once cumulative savings ≥ rebuild cost → RECOVERED (flash once, then silent).

Renders as a clean UI banner via `systemMessage`.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.cache_bust_state import latest_confirmed_bust, update_latest_bust
from lib.anatomy import get_context_window, get_model_rates


def _auto_detect_busts(session_path_str, all_usages, last_model):
    """Auto-detect cache busts not caught by cache_bust_warner (e.g. /model switch).

    Detection: a bust = prior turns had warm cache (mostly reads), then a turn
    where cache_creation >> cache_read AND cache_creation > 30K tokens.

    Guards against pollution:
      - skip detections older than 24h (don't resurrect ancient busts on resume)
      - ±600s dedup window against existing events
      - skip if computed actual cost < $0.02 (phantom)
    """
    import datetime as _dt
    import time as _time
    from lib.cache_bust_state import (
        all_events_for_session, record_bust, update_latest_bust,
        PHANTOM_THRESHOLD,
    )

    existing_events = all_events_for_session(session_path_str)
    existing_ts = {ev.get("ts", 0) for ev in existing_events}

    # Anything older than the latest resolved event is in the "past" of the
    # session's recovery timeline — don't insert orphans there.
    resolved_ts = [ev.get("ts", 0) for ev in existing_events
                   if ev.get("break_even_reached")]
    earliest_admissible_ts = max(resolved_ts) if resolved_ts else 0

    DEDUP_WINDOW_SEC = 600
    MAX_DETECTION_AGE_SEC = 24 * 3600
    now = _time.time()

    window = 3
    prev_usages = []
    prev_model = None

    for idx, u in enumerate(all_usages):
        usage = u["usage"]
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        inp = usage.get("input_tokens", 0)
        model = u.get("model") or prev_model or ""

        try:
            ts_str = u.get("ts", "")
            t = _dt.datetime.fromisoformat(
                ts_str.replace("Z", "+00:00")
            ).timestamp() if ts_str else 0
        except Exception:
            t = 0

        if idx >= window:
            w = prev_usages[-window:]
            w_reads = sum(x["usage"].get("cache_read_input_tokens", 0) for x in w)
            w_writes = sum(x["usage"].get("cache_creation_input_tokens", 0) for x in w)
            cache_was_warm = (w_reads > w_writes * 3) and w_reads > 10_000

            total = cr + cw + inp
            write_ratio = cw / total if total > 0 else 0

            if cache_was_warm and cw > 30_000 and write_ratio > 0.5:
                too_old = t > 0 and (now - t) > MAX_DETECTION_AGE_SEC
                in_resolved_past = t > 0 and t <= earliest_admissible_ts
                already_tracked = any(abs(ev_t - t) < DEDUP_WINDOW_SEC
                                      for ev_t in existing_ts)

                write_rate = get_model_rates(model)["cache_write"]
                actual_cost = round(cw / 1e6 * write_rate, 4)
                is_phantom = actual_cost < PHANTOM_THRESHOLD

                if (not already_tracked and not too_old and not is_phantom
                        and not in_resolved_past and t > 0):
                    action = "model_switch" if (prev_model and model != prev_model) else "cache_rebuilt"

                    record_bust(
                        session_path_str,
                        action_type=action,
                        target="auto-detected",
                        pre_bust_prefix=cr + cw + inp,
                        estimated_cost=actual_cost,
                        model=model,
                        ts=t,
                    )
                    update_latest_bust(session_path_str, {
                        "actual_rebuild_cost": actual_cost,
                        "rebuild_detected_ts": t,
                    }, target_ts=t)
                    existing_ts.add(t)
                    existing_events = all_events_for_session(session_path_str)

        prev_usages.append(u)
        prev_model = model or prev_model


def main():
    cwd = os.getcwd()
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return
    jsonls = sorted(project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonls:
        return
    session_path = jsonls[0]

    last_usage = None
    last_model = None
    all_usages = []
    user_msg_ts = []  # timestamps of real user messages (not tool_results)
    try:
        with open(session_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                # Capture assistant usages
                msg = d.get("message", {})
                if isinstance(msg, dict) and msg.get("usage"):
                    u = msg["usage"]
                    last_usage = u
                    last_model = msg.get("model")
                    all_usages.append({
                        "usage": u,
                        "ts": d.get("timestamp"),
                        "model": msg.get("model"),
                    })
                # Capture real user messages (not tool_result, not sidechain)
                if (d.get("type") == "user" and not d.get("isSidechain")
                        and isinstance(msg, dict)):
                    content = msg.get("content")
                    is_real = False
                    if isinstance(content, str):
                        is_real = not d.get("toolUseResult")
                    elif isinstance(content, list):
                        has_text = any(isinstance(c, dict) and c.get("type") == "text"
                                       for c in content)
                        has_tool_result = any(isinstance(c, dict) and c.get("type") == "tool_result"
                                              for c in content)
                        is_real = has_text and not has_tool_result
                    if is_real:
                        user_msg_ts.append(d.get("timestamp"))
    except Exception:
        return

    if not last_usage:
        return

    prefix = (last_usage.get("cache_read_input_tokens", 0)
              + last_usage.get("cache_creation_input_tokens", 0)
              + last_usage.get("input_tokens", 0))

    _auto_detect_busts(str(session_path), all_usages, last_model)

    window = get_context_window(last_model)

    threshold = window - 13_000
    pct = prefix / window * 100
    distance = max(0, threshold - prefix)

    filled = int(pct / 5)
    bar = "█" * filled + "░" * (20 - filled)
    suffix = "  → consider /compact" if pct >= 70 else ""

    lines = [
        f"📊 prefix: {prefix:,} / {window:,} "
        f"({pct:.1f}%) {bar} · {distance:,} to compact{suffix}"
    ]

    # Cache-bust recovery tracker (savings vs cold cache on the NEW model)
    bust = latest_confirmed_bust(str(session_path))
    if bust:
        import datetime as _dt
        new_model = bust.get("model") or last_model
        rates = get_model_rates(new_model)
        # Per-cached-token savings vs paying fresh input on the SAME model.
        # This is what the warm cache earns you each turn.
        savings_per_m = rates["input"] - rates["cache_read"]

        def _ts(s):
            try:
                return _dt.datetime.fromisoformat(
                    s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

        # Detect actual rebuild cost from first post-bust cache_creation spike,
        # priced at the NEW model's cache_write rate (not hardcoded Opus).
        if bust.get("actual_rebuild_cost") is None:
            bust_ts = bust.get("ts", 0)
            for u in all_usages:
                t = _ts(u.get("ts", ""))
                if t is None or t < bust_ts:
                    continue
                cw = u["usage"].get("cache_creation_input_tokens", 0)
                if cw > 0:
                    actual = cw / 1e6 * rates["cache_write"]
                    update_latest_bust(str(session_path), {
                        "actual_rebuild_cost": round(actual, 4),
                        "rebuild_detected_ts": t,
                    })
                    bust["actual_rebuild_cost"] = actual
                    bust["rebuild_detected_ts"] = t
                    break

        rebuild_ts = bust.get("rebuild_detected_ts") or bust.get("ts", 0)

        # Cumulative cache savings since rebuild
        cumulative_recovered = 0.0
        for u in all_usages:
            t = _ts(u.get("ts", ""))
            if t is None or t <= rebuild_ts:
                continue
            cache_reads = u["usage"].get("cache_read_input_tokens", 0)
            cumulative_recovered += cache_reads / 1e6 * savings_per_m

        # Count real user turns (one user message = one turn, regardless
        # of how many tool iterations the agent fires internally)
        user_turns = sum(1 for ts in user_msg_ts
                         if (_ts(ts) or 0) > rebuild_ts)

        rebuild_cost = bust.get("actual_rebuild_cost") or bust.get(
            "estimated_rebuild_cost", 0)
        if rebuild_cost and rebuild_cost > 0:
            action = bust.get("action_type", "bust").replace("_", " ")

            if cumulative_recovered >= rebuild_cost:
                # Cap at rebuild_cost so the stored field can't blow past 100%.
                update_latest_bust(str(session_path), {
                    "break_even_reached": True,
                    "break_even_at_turn": user_turns,
                    "post_bust_accumulated_cost": round(rebuild_cost, 4),
                    "post_bust_turn_count": user_turns,
                })
                bar = "█" * 10
                plural = "s" if user_turns != 1 else ""
                lines.append(
                    f"♻ {action}: [{bar}] ✓ RECOVERED "
                    f"(${rebuild_cost:.2f} rebuilt · earned back in {user_turns} user turn{plural})"
                )
            else:
                update_latest_bust(str(session_path), {
                    "post_bust_accumulated_cost": round(cumulative_recovered, 4),
                    "post_bust_turn_count": user_turns,
                })
                ratio = cumulative_recovered / rebuild_cost
                filled = int(ratio * 10)
                bar = "█" * filled + "░" * (10 - filled)

                # Rolling projection: turns to break-even at current rate
                projection = ""
                if user_turns > 0 and cumulative_recovered > 0:
                    rate = cumulative_recovered / user_turns
                    remaining_turns = (rebuild_cost - cumulative_recovered) / rate
                    projection = f" · ~{remaining_turns:.0f} more user turns to recover"

                lines.append(
                    f"♻ {action}: [{bar}] "
                    f"${cumulative_recovered:.2f}/${rebuild_cost:.2f} ({ratio*100:.0f}%)"
                    f"{projection}"
                )

    message = "\n".join(lines)
    print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()

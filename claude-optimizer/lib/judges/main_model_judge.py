"""Tier 3 judge — Sonnet structured verdict on compaction timing.

Only called when:
  • pressure ≥ 85%
  • >= 50 turns since last T3 call (session-scoped)
  • T1 + T2 are ambiguous

Sends ~500 input tokens, asks for ~50 output tokens. Cost: ~$0.05 per call.
Uses ANTHROPIC_API_KEY from env. Returns None on any failure (advisor stays silent).
"""

import json
import os
import re
import urllib.request


def ask_main_model(payload, model="claude-sonnet-4-6", timeout=30):
    """Return dict with {verdict: COMPACT_NOW|WAIT|SOON, reason: str} or None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    macro = " ".join(payload.get("macro_keywords", [])[:15]) or "(unknown)"
    prompts = payload.get("recent_user_prompts", [])[-5:]
    prompt_lines = "\n".join(
        f"  {i+1}. {(p.get('text') or '')[:180]}" for i, p in enumerate(prompts)
    )
    pressure = payload.get("pressure_pct", 0)
    window = payload.get("window", 200_000)

    user_msg = (
        f"You are a compaction-timing advisor for a coding session.\n"
        f"Compaction summarizes the conversation and reclaims ~70% of context, "
        f"but loses fine-grained detail — so it should fire at a task boundary, "
        f"not mid-implementation.\n\n"
        f"Session state:\n"
        f"  - Pressure: {pressure:.0f}% of {window:,}-token window\n"
        f"  - Macro task keywords (from first prompts): {macro}\n"
        f"  - Tools in current turn: {payload.get('tools_this_turn', 0)}\n"
        f"  - Tools in last 3 turns: {payload.get('tools_last_3_turns', 0)}\n"
        f"  - New file just read: {payload.get('new_file_read_recently', False)}\n"
        f"  - New skill loaded recently: {payload.get('new_skill_loaded_recently', False)}\n\n"
        f"Last 5 user prompts:\n{prompt_lines}\n\n"
        f"Decide whether NOW is a good moment to /compact. Output STRICTLY this JSON:\n"
        f'{{"verdict": "COMPACT_NOW" | "SOON" | "WAIT", "reason": "<one short sentence>"}}'
    )

    body = {
        "model": model,
        "max_tokens": 120,
        "messages": [{"role": "user", "content": user_msg}],
    }
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None

    text = ""
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            break

    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group())
    except Exception:
        return None

    verdict = parsed.get("verdict", "").upper()
    if verdict not in ("COMPACT_NOW", "SOON", "WAIT"):
        return None
    return {"verdict": verdict, "reason": parsed.get("reason", "")}

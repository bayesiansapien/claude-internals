"""Tier 2 judge — Haiku 4.5 as topic-continuity tiebreaker.

Replaces the prior Ollama-based judge. No local daemon, no RAM cost,
~$0.0006 per call.

Called only when the deterministic boundary score is ambiguous (4–6).
Uses ANTHROPIC_API_KEY env var; returns None on any failure.
"""

import json
import os
import urllib.request


def ask_haiku(prev_prompt, curr_prompt,
              model="claude-haiku-4-5-20251001",
              timeout=10):
    """Return True if topics DIFFER (boundary), False if SAME, None on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    user_msg = (
        f'Topic A: "{(prev_prompt or "")[:300]}"\n'
        f'Topic B: "{(curr_prompt or "")[:300]}"\n'
        f"Are these about the SAME ongoing task? Reply with one word: YES or NO."
    )

    body = {
        "model": model,
        "max_tokens": 8,
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
            text = block.get("text", "").strip().upper()
            break

    if not text:
        return None
    first_word = text.split()[0] if text.split() else ""
    if first_word.startswith("NO"):
        return True   # topics differ → boundary
    if first_word.startswith("YES"):
        return False  # same topic → not a boundary
    return None

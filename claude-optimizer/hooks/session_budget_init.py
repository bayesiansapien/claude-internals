#!/usr/bin/env python3
"""session-budget-init · SessionStart hook.

Fires once at the beginning of every Claude Code session. Does three things:

  1. Initializes per-session budget state at the configured default (3M tokens
     or whatever CC_SESSION_TOKEN_LIMIT env var says).

  2. If a continuation intent file is present at CC_INTENT_FILE env var or at
     /tmp/cc-session-intent-latest, surfaces it to the user as the session's
     opening context.

  3. Prints a one-line banner showing the budget so the user knows the gate
     value and can override with /budget <N>M.

The intent file is normally passed via `claude --append-system-prompt-file`,
which means the model already has the file content in its system prompt.
This hook's job here is just to surface a HUMAN-VISIBLE confirmation that
the carryover happened.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.session_budget_state import (
    init_session, DEFAULT_BUDGET,
)
from lib.hook_payload import read_hook_payload, resolve_session


def _format_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _find_intent_file():
    """Look for a continuation intent file. Priority order:
       1. $CC_INTENT_FILE env var
       2. <project-handoffs>/intent-latest (symlink, if used)
       3. Most recent <project-handoffs>/intent-*.md (within last hour)

    Handoff files live in `~/.claude/projects/<project-hash>/session-handoffs/`
    (per-project, persistent, hidden from git).
    """
    env_path = os.environ.get("CC_INTENT_FILE")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    cwd = os.getcwd()
    project_hash = cwd.replace("/", "-")
    handoff_dir = Path.home() / ".claude" / "projects" / project_hash / "session-handoffs"
    if not handoff_dir.exists():
        return None

    latest_symlink = handoff_dir / "intent-latest"
    if latest_symlink.exists():
        return latest_symlink

    candidates = sorted(
        handoff_dir.glob("intent-*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    # Only return if it's recent (last hour) — older files are stale
    import time
    if time.time() - candidates[0].stat().st_mtime < 3600:
        return candidates[0]
    return None


def main(payload=None):
    cwd = os.getcwd()
    session_uuid, _ = resolve_session(payload=payload, cwd=cwd)

    if not session_uuid:
        # Brand-new session that hasn't written its JSONL yet. We can't
        # initialize state without the UUID. The boundary advisor will
        # initialize it lazily on the first Stop hook fire.
        return

    state = init_session(session_uuid, project_cwd=cwd)
    budget = state["budget_tokens"]
    session_name = state.get("session_name")

    lines = []
    if session_name:
        lines.append(f"📛 SESSION NAME: {session_name}")
    lines.append(f"📋 SESSION BUDGET: {_format_tokens(budget)} tokens (budget-relevant: cache writes + output + fresh input)")
    lines.append(f"   Override with: /budget <N>M  (e.g. /budget 5M)")

    intent_file = _find_intent_file()
    if intent_file:
        # Resolve symlinks so the assistant gets the real file path
        real_path = intent_file.resolve()
        lines.append("")
        lines.append(f"📦 CONTINUATION DETECTED")
        lines.append(f"   This session inherits from a prior Claude Code session.")
        lines.append(f"   Handoff file: {real_path}")
        lines.append(f"")
        lines.append(f"   To bring the prior context in, use the Read tool on the")
        lines.append(f"   path above. The handoff contains:")
        lines.append(f"     • prior session metadata (turns, tokens, compactions)")
        lines.append(f"     • phase context (current + predicted next)")
        lines.append(f"     • last 5 user messages verbatim")
        lines.append(f"     • files in flight at the time of split")
        lines.append(f"")
        lines.append(f"   Auto-memory is already loaded (Tier 1 facts carry over).")
        lines.append(f"   Read the handoff only if you need extra reorientation.")

    banner = "\n".join(lines)
    print(json.dumps({"systemMessage": banner}))


if __name__ == "__main__":
    payload = read_hook_payload()
    main(payload=payload)

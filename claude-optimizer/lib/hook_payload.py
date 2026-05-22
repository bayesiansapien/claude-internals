"""Read the Claude Code hook payload from stdin.

CC passes hook context as JSON on stdin. The payload includes:
  - session_id        : the current session's UUID
  - transcript_path   : absolute path to this session's JSONL
  - cwd               : current working directory
  - hook_event_name   : which event fired (Stop, PreToolUse, SessionStart, ...)
  - tool_name / tool_input / tool_response : present for tool-related hooks

Using these is more reliable than mtime-based discovery, which breaks when
multiple Claude Code sessions run concurrently in the same project — the most
recently touched JSONL might belong to a DIFFERENT session.
"""

import json
import os
import sys
from pathlib import Path


def read_hook_payload():
    """Read the JSON payload from stdin. Returns dict (possibly empty)."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def resolve_session(payload=None, cwd=None):
    """Return (session_uuid, jsonl_path) for THIS hook invocation.

    Resolution order:
      1. payload['session_id'] — trust the UUID even if transcript_path isn't
         on disk yet (SessionStart fires BEFORE the JSONL is created).
      2. payload['transcript_path'] — derive UUID from the path stem.
      3. CC_SESSION_ID env var — set by some CC integration paths.
      4. Most recent JSONL in this project's dir (mtime-sorted) — last-resort
         fallback that is UNSAFE when multiple sessions in the same project
         are running concurrently.

    Returns (None, None) if no session can be found.
    """
    payload = payload or {}
    cwd = cwd or os.getcwd()

    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")

    # Path 1: payload's session_id — most reliable, even pre-JSONL creation
    sid = payload.get("session_id")
    tp = payload.get("transcript_path")
    if sid:
        # Prefer the payload's transcript_path; if missing, synthesize it
        if tp:
            return sid, Path(tp)
        candidate = project_dir / f"{sid}.jsonl"
        return sid, candidate

    # Path 2: only transcript_path was given — derive sid from its stem
    if tp:
        return Path(tp).stem, Path(tp)

    # Path 3: env var
    sid_env = os.environ.get("CC_SESSION_ID")
    if sid_env:
        candidate = project_dir / f"{sid_env}.jsonl"
        return sid_env, candidate

    # Path 4: mtime fallback (LAST RESORT — unsafe with concurrent sessions)
    if not project_dir.exists():
        return None, None
    jsonls = sorted(project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonls:
        return None, None
    return jsonls[0].stem, jsonls[0]

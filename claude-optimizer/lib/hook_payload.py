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
      1. payload['transcript_path'] + payload['session_id'] — most reliable,
         points to the exact session this hook fired for.
      2. CC_SESSION_ID env var — set by some CC integration paths.
      3. Most recent JSONL in this project's dir (mtime-sorted) — fallback.

    Returns (None, None) if no session can be found.
    """
    payload = payload or {}
    cwd = cwd or os.getcwd()

    # Path 1: payload provides real session info
    tp = payload.get("transcript_path")
    sid = payload.get("session_id")
    if tp and Path(tp).exists():
        if not sid:
            sid = Path(tp).stem
        return sid, Path(tp)

    # Path 2: env var
    sid_env = os.environ.get("CC_SESSION_ID")
    if sid_env:
        project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
        candidate = project_dir / f"{sid_env}.jsonl"
        if candidate.exists():
            return sid_env, candidate

    # Path 3: mtime fallback
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    if not project_dir.exists():
        return None, None
    jsonls = sorted(project_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonls:
        return None, None
    return jsonls[0].stem, jsonls[0]

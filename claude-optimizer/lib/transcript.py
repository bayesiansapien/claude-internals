"""Transcript parsing utilities for Claude Code session jsonl files."""

import json
import os
import glob
from pathlib import Path


PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def project_hash_from_cwd(cwd=None) :
    """Convert a working directory path to Claude Code's project-hash convention.

    Path is encoded by replacing / with -.
    """
    cwd = cwd or os.getcwd()
    abs_path = os.path.abspath(cwd)
    return abs_path.replace("/", "-")


def project_dir(cwd=None) -> Path:
    """Return the ~/.claude/projects/<hash>/ directory for a working dir."""
    return PROJECTS_ROOT / project_hash_from_cwd(cwd)


def find_session_files(cwd=None) :
    """Find all session jsonl files for the current project, newest first."""
    pdir = project_dir(cwd)
    if not pdir.exists():
        return []
    files = list(pdir.glob("*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def current_session_file(cwd=None) :
    """Return the most-recently modified session jsonl for the current project."""
    files = find_session_files(cwd)
    return files[0] if files else None


def iter_messages(path: Path):
    """Yield parsed message dicts from a session jsonl file."""
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def extract_usage(record: dict) :
    """Extract (usage, model) from a session record. Returns (None, None) if absent."""
    msg = record.get("message", {})
    if not isinstance(msg, dict):
        return None, None
    usage = msg.get("usage")
    model = msg.get("model")
    return usage, model


def categorize_record(record: dict) :
    """Bucket a record into main loop / background / subagent / meta.

    Categorization rules (in priority order):
      1. agentType present  → subagent:<type>
      2. isSidechain == True → background (legacy sidechain)
      3. message text contains autoMemory markers → auto memory
      4. isMeta == True → meta
      5. otherwise → main loop
    """
    if record.get("agentType"):
        return f"subagent:{record['agentType']}"
    if record.get("isSidechain"):
        return "background (sidechain)"

    # auto-memory detection: scan the raw message for known markers
    raw = json.dumps(record)
    if any(m in raw for m in ("autoMemory", "Writing memory", "Recalled memory",
                              "memory/MEMORY.md", "memoryWrite", "memoryRead")):
        return "auto memory"

    if record.get("isMeta"):
        return "meta (system)"

    return "main loop"


def latest_usage(path: Path) :
    """Get the latest message usage in the session. Returns (usage, model, line_num) or None."""
    last = None
    line_num = 0
    for i, record in enumerate(iter_messages(path)):
        usage, model = extract_usage(record)
        if usage:
            last = (usage, model, i)
            line_num = i
    return last

#!/usr/bin/env python3
"""memory-hygiene · Audit ~/.claude/projects/<hash>/memory/ for size, staleness, and redundancy.

Reports:
  • File-by-file size, line count, last modified
  • Total memory directory size and estimated token cost per session
  • Stale or oversized files flagged
  • MEMORY.md index health (200-line / 25KB cap)
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.transcript import project_dir


CHARS_PER_TOKEN = 4  # rough estimate for English text


def estimate_tokens(byte_size):
    return byte_size // CHARS_PER_TOKEN


def days_since(timestamp_seconds):
    """Days since a Unix timestamp."""
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
    return age.days


def render(memory_dir):
    print()
    print(f"  MEMORY HYGIENE · {memory_dir}")
    print("  " + "─" * 74)

    if not memory_dir.exists():
        print("  No memory directory exists for this project.")
        print(f"  Path would be: {memory_dir}")
        print()
        return

    files = sorted(memory_dir.glob("*.md"))
    if not files:
        print("  Memory directory is empty.")
        print()
        return

    print(f"  {'File':<32} {'Size':>7} {'Lines':>6} {'Age':>8}  Flags")
    print("  " + "─" * 74)

    total_size = 0
    total_lines = 0
    flagged = []
    memory_md_size = 0
    memory_md_lines = 0

    for f in files:
        stat = f.stat()
        size = stat.st_size
        try:
            lines = sum(1 for _ in open(f, 'r', encoding='utf-8', errors='replace'))
        except Exception:
            lines = 0
        age_days = days_since(stat.st_mtime)

        flags = []
        if f.name == "MEMORY.md":
            memory_md_size = size
            memory_md_lines = lines
            if lines > 200:
                flags.append("⚠ exceeds 200-line cap")
            if size > 25_000:
                flags.append("⚠ exceeds 25KB cap")
        else:
            if size > 5_000:
                flags.append("⚠ large (>5KB)")
            if age_days > 30:
                flags.append("⚠ stale (>30d)")

        total_size += size
        total_lines += lines
        if flags:
            flagged.append((f.name, flags))

        size_str = f"{size:,}B"
        age_str = f"{age_days}d" if age_days < 1000 else "—"
        flag_str = " ".join(flags) if flags else ""
        print(f"  {f.name:<32} {size_str:>7} {lines:>6} {age_str:>8}  {flag_str}")

    print("  " + "─" * 74)
    print(f"  Total: {len(files)} files · {total_size:,} bytes · ~{estimate_tokens(total_size):,} tokens")
    print()
    print(f"  Per-session impact: ~{estimate_tokens(total_size):,} tokens loaded into the system prompt")
    print(f"  Cached at 0.10× input rate after turn 1 — but every byte counts every session.")
    print()

    insights = []
    if memory_md_size > 25_000:
        insights.append(f"MEMORY.md is {memory_md_size:,}B — exceeds the 25KB cap; trim it")
    if memory_md_lines > 200:
        insights.append(f"MEMORY.md has {memory_md_lines} lines — exceeds the 200-line cap")
    if flagged:
        stale_count = sum(1 for _, fl in flagged if any("stale" in s for s in fl))
        large_count = sum(1 for _, fl in flagged if any("large" in s for s in fl))
        if stale_count:
            insights.append(f"{stale_count} stale file(s) — review and archive what's no longer relevant")
        if large_count:
            insights.append(f"{large_count} large file(s) — consider splitting or condensing")
    if total_size > 50_000:
        insights.append(f"Total memory is {total_size//1000}KB — significant per-session token cost")

    if insights:
        print(f"  💡 INSIGHTS")
        for ins in insights:
            print(f"     • {ins}")
        print()


def main():
    render(project_dir() / "memory")


if __name__ == "__main__":
    main()

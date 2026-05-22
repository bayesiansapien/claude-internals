#!/usr/bin/env python3
"""session-launcher · Auto-spawn a new Claude Code session in a fresh
terminal tab, carrying the handoff intent file as system prompt context.

Usage:
  python3 session_launcher.py --intent <path> [--name <session-name>] [--cwd <dir>]

Behavior:
  - On macOS with iTerm2: opens a new iTerm window and runs `claude` with
    --append-system-prompt-file <intent>.
  - On macOS with Apple Terminal: opens a new Terminal window with the same.
  - On other terminals or non-macOS: prints the exact bash one-liner the user
    can paste into a new terminal of their choice.

Never auto-launches without explicit invocation. The boundary advisor will
print this script's invocation as a recommendation, but the user (or model
on user's confirmation) is what triggers it.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _terminal_program():
    return os.environ.get("TERM_PROGRAM", "")


def _build_command(cwd, intent_path, session_name=None):
    """Construct the bash command that will run inside the new tab/window.

    The launched session is intentionally an ORGANIC Claude Code session:
    - just `cd <cwd> && claude`
    - all user-level settings, user-level CLAUDE.md, user-level skills load normally
    - all project-level settings, project CLAUDE.md, project skills load normally
    - auto-memory loads normally from ~/.claude/projects/<hash>/memory/
    - SessionStart hooks fire normally

    The intent file is NOT injected into the system prompt. It lives in the
    project's session-handoffs/ directory, and the SessionStart hook surfaces
    its path so the assistant can Read it on demand. This keeps the new session
    architecturally identical to one you would launch by hand.
    """
    parts = []
    if session_name:
        parts.append(f"export CC_SESSION_TITLE={shlex_quote(session_name)}")
    # If an intent file exists, expose its path via env var so the new
    # session's SessionStart hook can find it specifically (in addition to
    # the symlink-based discovery)
    if intent_path:
        parts.append(f"export CC_INTENT_FILE={shlex_quote(str(intent_path))}")
    parts.append(f"cd {shlex_quote(cwd)}")
    parts.append("claude")
    return " && ".join(parts)


def shlex_quote(s):
    """Conservative shell-quote (handles spaces and special chars)."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_/.:@=+%" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _launch_iterm(command, theme=None):
    """Open a new iTerm2 window and run the command.

    If theme is provided, opens with that profile. iTerm profiles are managed
    in Preferences → Profiles. If the profile doesn't exist, iTerm falls back
    to default.
    """
    escaped = command.replace('"', '\\"')
    profile_clause = (
        f'    create window with profile "{theme}"\n'
        if theme else
        '    create window with default profile\n'
    )
    osascript = (
        'tell application "iTerm"\n'
        '    activate\n'
        + profile_clause +
        '    tell current session of current window\n'
        f'        write text "{escaped}"\n'
        '    end tell\n'
        'end tell\n'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", osascript],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0, result.stderr
    except Exception as e:
        return False, str(e)


def _launch_apple_terminal(command, theme=None):
    """Open a new Apple Terminal window and run the command.

    If theme is provided, applies the named Settings Set (e.g. "Clear Dark",
    "Pro", "Homebrew"). Settings Sets are managed in Terminal → Preferences →
    Profiles. If the theme name doesn't match any profile, the window opens
    with the current default and an error is printed to stderr (non-fatal).
    """
    escaped = command.replace('"', '\\"')
    if theme:
        theme_escaped = theme.replace('"', '\\"')
        osascript = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    set newTab to do script "{escaped}"\n'
            f'    set current settings of newTab to settings set "{theme_escaped}"\n'
            'end tell\n'
        )
    else:
        osascript = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    do script "{escaped}"\n'
            'end tell\n'
        )
    try:
        result = subprocess.run(
            ["osascript", "-e", osascript],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0, result.stderr
    except Exception as e:
        return False, str(e)


def _print_manual_command(command):
    """Fallback: tell the user the exact command to paste."""
    print()
    print("┌─" + "─" * 70 + "─┐")
    print("│  Auto-launch is not available for this terminal.")
    print("│  Open a new terminal tab and paste this command:")
    print("│")
    print(f"│    {command}")
    print("│")
    print("└─" + "─" * 70 + "─┘")
    print()


def _project_handoff_dir(cwd=None):
    """Return the per-project handoff directory for this cwd."""
    cwd = cwd or os.getcwd()
    project_hash = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / project_hash / "session-handoffs"


def latest_intent_file(cwd=None):
    """Find the most recent intent file in the project-handoffs directory."""
    d = _project_handoff_dir(cwd)
    if not d.exists():
        return None
    candidates = sorted(
        d.glob("intent-*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Skip the symlink itself
    candidates = [p for p in candidates if not p.is_symlink()]
    return candidates[0] if candidates else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", default=None,
                    help="Path to the handoff intent file (markdown). "
                         "If omitted, uses the most recent /tmp/cc-session-intent-*.md")
    ap.add_argument("--name", default=None,
                    help="Suggested name for the new session (informational)")
    ap.add_argument("--cwd", default=None,
                    help="Working directory for the new session (defaults to current)")
    ap.add_argument("--theme", default=None,
                    help="Terminal theme/profile to use (e.g. 'Clear Dark' for "
                         "Apple Terminal Settings Set, or an iTerm profile name). "
                         "Defaults to whatever the terminal opens with normally. "
                         "Can also be set via env var CC_LAUNCHER_THEME.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the command that would be run, don't launch")
    args = ap.parse_args()

    # Theme can also come from env var
    theme = args.theme or os.environ.get("CC_LAUNCHER_THEME")

    cwd = args.cwd or os.getcwd()
    intent_path = args.intent
    if intent_path is None:
        latest = latest_intent_file(cwd)
        if latest:
            intent_path = str(latest)

    if intent_path and not Path(intent_path).exists():
        print(f"⚠ Intent file not found: {intent_path}", file=sys.stderr)
        print("   Launching without continuation context.", file=sys.stderr)
        intent_path = None

    # Maintain an "intent-latest" symlink inside the per-project handoffs
    # directory so the SessionStart hook in the new session finds the right
    # file even without us passing CC_INTENT_FILE.
    if intent_path:
        handoff_dir = _project_handoff_dir(cwd)
        handoff_dir.mkdir(parents=True, exist_ok=True)
        latest_link = handoff_dir / "intent-latest"
        try:
            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            latest_link.symlink_to(Path(intent_path).resolve())
        except Exception:
            pass

    command = _build_command(cwd, intent_path, args.name)

    if args.dry_run:
        print(command)
        return

    if not shutil.which("claude"):
        print("⚠ `claude` not found in PATH. Cannot launch.", file=sys.stderr)
        _print_manual_command(command)
        sys.exit(1)

    # Cross-platform launch via the platform_compat abstraction.
    # Handles macOS (iTerm/Apple Terminal), Linux (gnome-terminal, kitty,
    # konsole, xterm), and Windows (Windows Terminal, cmd).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lib.platform_compat import launch_in_new_terminal, _detect_terminal

    detected = _detect_terminal()
    print(f"Platform:          {sys.platform}", file=sys.stderr)
    print(f"Detected terminal: {detected}", file=sys.stderr)
    print(f"New session name:  {args.name or '(none)'}", file=sys.stderr)
    print(f"Working dir:       {cwd}", file=sys.stderr)
    print(f"Intent file:       {intent_path or '(none)'}", file=sys.stderr)
    print(f"Theme:             {theme or '(terminal default)'}", file=sys.stderr)

    result = launch_in_new_terminal(command, theme=theme)

    if result.ok:
        print(f"✓ New window opened via {result.mechanism}. Switch to it to continue.")
        if result.error and result.error.strip():
            # Non-fatal warnings (e.g. theme name didn't match a profile)
            print(f"   (note: {result.error.strip()})", file=sys.stderr)
    else:
        print(f"⚠ Auto-launch failed ({result.mechanism}): {result.error}",
              file=sys.stderr)
        _print_manual_command(command)


if __name__ == "__main__":
    main()

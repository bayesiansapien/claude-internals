"""Cross-platform compatibility helpers for claude-optimizer.

This module centralizes the two places the toolkit needs to do OS-specific
work:

  1. read_oauth_token()         — pull the Claude Code OAuth token from the
                                  user's system credential store (Keychain on
                                  macOS, Secret Service on Linux, Credential
                                  Manager on Windows).

  2. launch_in_new_terminal()   — spawn a new Claude Code session in a fresh
                                  terminal window/tab on the user's preferred
                                  terminal emulator.

Both functions degrade gracefully:
  - If the `keyring` Python library is installed, it is used first (most
    portable and reliable). Otherwise the function tries OS-native commands.
  - If no terminal can be auto-launched, the launcher returns the bash command
    so the user can paste it into a terminal of their choosing.

No hard dependencies. Both `keyring` and the OS-native commands are tried in
turn; the toolkit works (degraded) even if all fail.
"""

import os
import shutil
import subprocess
import sys
from typing import Optional


# ---------------- OAuth token retrieval ----------------

# The Claude Code OAuth credential is stored under these identifiers in
# each platform's credential store.
KEYCHAIN_SERVICE_NAME = "Claude Code-credentials"


def _read_oauth_via_keyring() -> Optional[str]:
    """Try the cross-platform `keyring` library. Returns the credential
    string or None if the library is unavailable or the entry isn't found."""
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    try:
        # On macOS, the keyring library reads from the Keychain. On Linux,
        # it talks to Secret Service via D-Bus. On Windows, it queries the
        # Credential Manager. All three return the same format.
        cred = keyring.get_password(KEYCHAIN_SERVICE_NAME, "")
        return cred
    except Exception:
        # Some Linux distros lack a running Secret Service or its D-Bus
        # interface. In that case keyring raises; we fall through.
        return None


def _read_oauth_via_macos() -> Optional[str]:
    """macOS native: `security find-generic-password -w`."""
    if sys.platform != "darwin":
        return None
    if not shutil.which("security"):
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE_NAME, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _read_oauth_via_secret_tool() -> Optional[str]:
    """Linux native: `secret-tool` (libsecret). Available on most modern
    distros with GNOME/KDE keyrings."""
    if sys.platform != "linux":
        return None
    if not shutil.which("secret-tool"):
        return None
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _read_oauth_via_powershell() -> Optional[str]:
    """Windows native: PowerShell + CredentialManager module.

    Requires either:
      - PowerShell module `CredentialManager` installed
      - OR Windows 10+ with `Get-StoredCredential`

    Falls through to None if neither is available.
    """
    if sys.platform != "win32":
        return None
    if not shutil.which("powershell.exe") and not shutil.which("pwsh"):
        return None
    ps_cmd = (
        '$cred = Get-StoredCredential -Target "' + KEYCHAIN_SERVICE_NAME + '"; '
        'if ($cred) { $cred.GetNetworkCredential().Password }'
    )
    try:
        powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def read_oauth_token() -> Optional[str]:
    """Return the Claude Code OAuth token (raw credential string).

    Resolution order:
      1. `keyring` Python library (works on all 3 OSes if installed)
      2. OS-native fallback (macOS: security, Linux: secret-tool, Windows: PS)
      3. None — caller must handle gracefully

    The returned value is a JSON string like:
      '{"claudeAiOauth":{"accessToken":"...","scopes":[...]}}'

    Callers should json.loads() it and pull the field they need.
    """
    for fn in (
        _read_oauth_via_keyring,
        _read_oauth_via_macos,
        _read_oauth_via_secret_tool,
        _read_oauth_via_powershell,
    ):
        result = fn()
        if result:
            return result
    return None


# ---------------- Terminal launching ----------------

class LaunchResult:
    """Return value from launch_in_new_terminal."""
    def __init__(self, ok: bool, mechanism: str, error: str = ""):
        self.ok = ok
        self.mechanism = mechanism  # e.g. "iterm-osascript", "gnome-terminal"
        self.error = error


def _detect_terminal() -> str:
    """Return a short identifier for the active terminal program."""
    if sys.platform == "darwin":
        term = os.environ.get("TERM_PROGRAM", "")
        if "iTerm" in term:
            return "iterm"
        if "Apple" in term or "Terminal" in term:
            return "apple-terminal"
        return "macos-unknown"
    if sys.platform == "linux":
        # Most reliable signals on Linux
        if os.environ.get("KITTY_WINDOW_ID"):
            return "kitty"
        if os.environ.get("ALACRITTY_LOG"):
            return "alacritty"
        if shutil.which("gnome-terminal"):
            return "gnome-terminal"
        if shutil.which("konsole"):
            return "konsole"
        if shutil.which("xterm"):
            return "xterm"
        return "linux-unknown"
    if sys.platform == "win32":
        # Windows Terminal vs cmd.exe vs PowerShell
        if "WT_SESSION" in os.environ:
            return "windows-terminal"
        return "windows-unknown"
    return "unknown"


def _osascript_iterm(command: str, theme: Optional[str] = None) -> LaunchResult:
    escaped = command.replace('"', '\\"')
    profile_line = (
        f'    create window with profile "{theme}"\n'
        if theme else
        '    create window with default profile\n'
    )
    script = (
        'tell application "iTerm"\n'
        '    activate\n'
        + profile_line +
        '    tell current session of current window\n'
        f'        write text "{escaped}"\n'
        '    end tell\n'
        'end tell\n'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "iterm-osascript", r.stderr)
    except Exception as e:
        return LaunchResult(False, "iterm-osascript", str(e))


def _osascript_apple_terminal(command: str, theme: Optional[str] = None) -> LaunchResult:
    escaped = command.replace('"', '\\"')
    if theme:
        theme_esc = theme.replace('"', '\\"')
        script = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    set newTab to do script "{escaped}"\n'
            f'    set current settings of newTab to settings set "{theme_esc}"\n'
            'end tell\n'
        )
    else:
        script = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    do script "{escaped}"\n'
            'end tell\n'
        )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "apple-terminal-osascript", r.stderr)
    except Exception as e:
        return LaunchResult(False, "apple-terminal-osascript", str(e))


def _linux_gnome_terminal(command: str, theme: Optional[str] = None) -> LaunchResult:
    # gnome-terminal supports profile selection via --profile
    args = ["gnome-terminal"]
    if theme:
        args += ["--profile", theme]
    args += ["--", "bash", "-c", f"{command}; exec bash"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "gnome-terminal", r.stderr)
    except Exception as e:
        return LaunchResult(False, "gnome-terminal", str(e))


def _linux_konsole(command: str, theme: Optional[str] = None) -> LaunchResult:
    args = ["konsole"]
    if theme:
        args += ["--profile", theme]
    args += ["-e", "bash", "-c", f"{command}; exec bash"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "konsole", r.stderr)
    except Exception as e:
        return LaunchResult(False, "konsole", str(e))


def _linux_kitty(command: str, theme: Optional[str] = None) -> LaunchResult:
    # kitty supports remote control if enabled; otherwise spawn a new instance
    # (theme is set via --override which works in detached mode)
    args = ["kitty"]
    if theme:
        args += ["--override", f"theme={theme}"]
    args += ["bash", "-c", f"{command}; exec bash"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "kitty", r.stderr)
    except Exception as e:
        return LaunchResult(False, "kitty", str(e))


def _linux_xterm(command: str, theme: Optional[str] = None) -> LaunchResult:
    args = ["xterm", "-e", "bash", "-c", f"{command}; exec bash"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "xterm", r.stderr)
    except Exception as e:
        return LaunchResult(False, "xterm", str(e))


def _windows_terminal(command: str, theme: Optional[str] = None) -> LaunchResult:
    # Windows Terminal: `wt new-tab -p "ProfileName" cmd /c <command>`
    if not shutil.which("wt.exe") and not shutil.which("wt"):
        return LaunchResult(False, "windows-terminal", "wt.exe not in PATH")
    wt = shutil.which("wt.exe") or shutil.which("wt")
    args = [wt, "new-tab"]
    if theme:
        args += ["-p", theme]
    args += ["cmd", "/c", command + " && pause"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return LaunchResult(r.returncode == 0, "windows-terminal", r.stderr)
    except Exception as e:
        return LaunchResult(False, "windows-terminal", str(e))


def _windows_cmd(command: str, theme: Optional[str] = None) -> LaunchResult:
    # Fall back to `start cmd /k <command>` — opens a new cmd window
    try:
        r = subprocess.run(
            ["cmd", "/c", "start", "cmd", "/k", command],
            capture_output=True, text=True, timeout=10,
        )
        return LaunchResult(r.returncode == 0, "cmd", r.stderr)
    except Exception as e:
        return LaunchResult(False, "cmd", str(e))


def launch_in_new_terminal(command: str, theme: Optional[str] = None) -> LaunchResult:
    """Launch a command in a new terminal window/tab on the user's OS.

    On macOS, dispatches to iTerm2 (via osascript) or Apple Terminal based on
    $TERM_PROGRAM.

    On Linux, tries kitty / gnome-terminal / konsole / xterm in that order.

    On Windows, prefers Windows Terminal (`wt.exe`), falls back to cmd.

    Returns a LaunchResult with ok=False if no supported terminal was found.
    Caller should check `.ok` and print the command for the user to paste if
    the launch failed.
    """
    term = _detect_terminal()

    if term == "iterm":
        return _osascript_iterm(command, theme)
    if term == "apple-terminal":
        return _osascript_apple_terminal(command, theme)
    if term == "macos-unknown":
        # macOS but unrecognized terminal — try Apple Terminal as a safe default
        return _osascript_apple_terminal(command, theme)
    if term == "kitty":
        return _linux_kitty(command, theme)
    if term in ("gnome-terminal",):
        return _linux_gnome_terminal(command, theme)
    if term == "konsole":
        return _linux_konsole(command, theme)
    if term in ("xterm", "linux-unknown"):
        # Try in order of preference on generic Linux
        for fn in (_linux_gnome_terminal, _linux_konsole, _linux_kitty, _linux_xterm):
            r = fn(command, theme)
            if r.ok:
                return r
        return LaunchResult(False, "linux-none", "No supported Linux terminal found")
    if term == "windows-terminal":
        return _windows_terminal(command, theme)
    if term in ("windows-unknown",):
        # Try Windows Terminal first, fall back to cmd
        r = _windows_terminal(command, theme)
        if r.ok:
            return r
        return _windows_cmd(command, theme)

    return LaunchResult(False, "unknown", f"Unknown terminal type: {term}")


if __name__ == "__main__":
    # CLI smoke test
    print(f"Platform:         {sys.platform}")
    print(f"Detected terminal: {_detect_terminal()}")
    print(f"keyring library:   {'available' if _read_oauth_via_keyring() is not None else 'not available or empty'}")
    print(f"OAuth token read:  {'✓ found' if read_oauth_token() else '✗ not found'}")

"""`tycho status` — the one-line "am I on?" indicator, for a harness status bar.

Harness contract (Claude Code 2.1.210, read from the shipped binary — see
the pinned harness contract): status payload arrives as JSON on **stdin**, ~5s budget;
**stdout** is used only on **exit 0**, trimmed, blank lines dropped; ANSI colour is
rendered dimmed; stderr goes to a debug log.

Reads only what the hook already wrote to disk (`state`) — never verifies inline, imports
no engine, and fails open to empty output, which renders as nothing at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import state

_RESET = "\033[0m"


def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


# Desaturated from pure ANSI: Claude Code renders the status line dark and dimmed, where
# full-saturation glares. The badge text never changes; the colour carries the status.
_GREEN = _rgb(63, 224, 87)     # VERIFIED        — proven good
_RED = _rgb(224, 138, 138)     # FAILED / STALE  — adverse
_YELLOW = _rgb(216, 194, 130)  # INDETERMINATE   — ran, couldn't conclude
_FROST = _rgb(172, 213, 243)   # verifying now   — transient, mid-run
_TEAL = _rgb(111, 201, 192)    # OVERRIDDEN      — agent-authorized, not proven
_GREY = "\033[90m"             # no signal       — never fired / UNSUPPORTED / nothing to verify

_VERDICT_COLOUR = {"VERIFIED": _GREEN, "FAILED": _RED, "STALE": _RED,
                   "INDETERMINATE": _YELLOW, "OVERRIDDEN": _TEAL}


def line(repo: Path) -> str:
    """The indicator for `repo`, or "" when there's nothing honest to show."""
    if not state.read_install(repo):
        return ""  # not installed here
    if os.environ.get("TYCHO_STATUS", "").strip().lower() in ("0", "off", "false", "no"):
        return ""  # global override
    if not state.status_enabled(repo):
        return ""  # hidden in this repo — the hook still runs, only the badge hides
    beat = state.last_run(repo) or {}
    if not isinstance(beat.get("at"), (int, float)):
        return _paint(_GREY, "[TYCHO]")  # installed, never fired here
    verdict = beat.get("verdict")
    if verdict is None:
        # Mid-run is frost "verifying"; a completed run with nothing to verify is grey —
        # not a false "working forever".
        return _paint(_FROST if beat.get("pending") else _GREY, "[TYCHO]")
    return _paint(_VERDICT_COLOUR.get(verdict, _GREY), "[TYCHO]")


def _paint(colour: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{colour}{text}{_RESET}"


def repo_of(payload: dict) -> Path:
    """Which repo the bar is drawn for: `workspace.project_dir` (where `.tycho/` lives),
    else payload `cwd`, else ours for a human running this by hand."""
    workspace = payload.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("project_dir"), str):
        return Path(workspace["project_dir"])
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) else Path.cwd()


def _stdin_text() -> str:
    """The harness's JSON on stdin as raw text — or "" when a human ran this in a terminal.

    Raw, not parsed: a wrapped status command needs the exact same bytes forwarded to it.
    The isatty check keeps an interactive `tycho status` from blocking on a read.
    """
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read() or ""
    except (OSError, ValueError):
        return ""


def _parse(text: str) -> dict:
    try:
        payload = json.loads(text or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _wrapped_output(repo: Path, stdin_text: str) -> str:
    """Run the status command Tycho is composing with, and return its line.

    statusLine holds one slot, so Tycho becomes the slot and runs the neighbour itself with
    the same stdin payload. Fail-open to "": a broken neighbour can't take the line down.
    """
    wrap = state.read_statusline_wrap(repo)
    command = wrap.get("command") if isinstance(wrap, dict) else None
    if not isinstance(command, str) or not command:
        return ""
    try:
        proc = subprocess.run(
            command, shell=True, input=stdin_text, capture_output=True,
            text=True, timeout=3, encoding="utf-8", errors="replace",
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def main(off: bool = False, on: bool = False) -> int:
    try:
        # Output is UTF-8; on Windows a piped stdout defaults to cp1252 and raises
        # UnicodeEncodeError. reconfigure may be absent on a wrapped stdout, hence the catch.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = _stdin_text()
    repo = repo_of(_parse(raw))
    if off or on:
        state.set_status_enabled(repo, enabled=on)
        print(f"tycho: status indicator {'shown' if on else 'hidden'} for {repo}"
              f"{'' if on else ' — the hook keeps verifying; run `tycho status --on` to show it'}")
        return 0
    try:
        segments = [_wrapped_output(repo, raw), line(repo)]
        text = " ".join(s for s in segments if s)
        if text:
            print(text)
    except Exception:
        # Fail open: this draws on every terminal render — empty output + exit 0.
        pass
    return 0

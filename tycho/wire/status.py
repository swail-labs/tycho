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

from ..store import state

from ..views.colour import _FROST, _GREY, _RESET, _VERDICT_COLOUR


# The mark beside the name, so the badge does not encode its state in colour alone. Colour is
# unavailable more often than it looks: `NO_COLOR` is honoured right below, a status bar may
# strip ANSI, and red against green is the pair colourblind readers most often cannot separate
# — worse here, since the harness renders status-line colour dimmed. Without a mark, all five
# states rendered the identical string and the badge told those readers nothing at all.
#
# Keep every entry in step with `_VERDICT_COLOUR`; a test pins the two together.
_VERDICT_MARK = {"VERIFIED": "✓", "FAILED": "✗", "STALE": "✗", "INDETERMINATE": "?",
                 "OVERRIDDEN": "~"}
_PENDING_MARK = "…"


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
        return _badge(_GREY, "")  # installed, never fired here
    verdict = beat.get("verdict")
    if verdict is None:
        # Mid-run is frost "verifying"; a completed run with nothing to verify is grey —
        # not a false "working forever".
        return _badge(_FROST, _PENDING_MARK) if beat.get("pending") else _badge(_GREY, "")
    # An unrecognized verdict is no signal, and says so both ways rather than guessing a mark.
    return _badge(_VERDICT_COLOUR.get(verdict, _GREY), _VERDICT_MARK.get(verdict, ""))


def _badge(colour: str, mark: str) -> str:
    return _paint(colour, f"[TYCHO {mark}]" if mark else "[TYCHO]")


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
    for stream in (sys.stdin, sys.stdout):
        try:
            # On Windows a pipe defaults to cp1252, which raises on output and mojibakes the
            # repo path on input. `reconfigure` may be absent on a wrapped stream.
            stream.reconfigure(encoding="utf-8", errors="replace")
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

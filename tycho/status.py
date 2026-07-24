"""`tycho status` — the one-line "am I on?" indicator, for a harness status bar (TYCHO-39).

`doctor` answers the same question, but only when asked. A verifier nobody can see is a
verifier that gets silently trusted while dead, so this is the passive form: the harness
renders it in the terminal on every draw, and the user never has to run a command to know
they're covered.

The contract it's written against (Claude Code 2.1.210, read from the shipped binary —
see `docs/harness-support.md`):

- the command is spawned with the status payload as JSON on **stdin**, and gets ~5s
  before it's aborted;
- **stdout** is used only on **exit 0**, trimmed, blank lines dropped;
- ANSI colour is supported and rendered dimmed; stderr is swallowed to a debug log.

So this module reads *only* what the hook already wrote to disk (`state`), never verifies
inline, imports no engine, and fails open to empty output — an empty line renders as
nothing at all, which is the correct thing to show when we can't prove we're live.
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


# Subdued, readable-on-black palette (TYCHO-94). Truecolor: the frost-blue mid-run beat is its
# exact shade, and the verdict colours are desaturated from pure ANSI — Claude Code renders the
# status line dark and dimmed, where full-saturation ANSI glares and is hard to read. Grey stays
# the terminal's own bright-black: the honest "no signal", deliberately left as-is.
_GREEN = _rgb(63, 224, 87)     # VERIFIED        — clear green (distinct from OVERRIDDEN teal)
_RED = _rgb(224, 138, 138)     # FAILED / STALE  — muted rose (both adverse)
_YELLOW = _rgb(216, 194, 130)  # INDETERMINATE   — muted amber (ran, couldn't conclude)
_FROST = _rgb(172, 213, 243)   # verifying now   — frost blue #ACD5F3 (transient, mid-run)
_TEAL = _rgb(111, 201, 192)    # OVERRIDDEN      — teal (agent-authorized, not proven)
_GREY = "\033[90m"             # no signal       — never fired / UNSUPPORTED / nothing to verify

# Five honest states for the `[TYCHO]` badge (TYCHO-47/59/94). The text never changes; the
# colour carries the status:
#   green  = VERIFIED            — proven good
#   red    = FAILED / STALE      — adverse, something to look at
#   frost  = verifying (pending) — a run is in flight this turn
#   yellow = INDETERMINATE       — ran but couldn't conclude (attention, not alarm)
#   grey   = no signal           — never fired, UNSUPPORTED, or a completed run with nothing to verify
# It lands on green or red once a verdict exists; frost is only the in-flight moment, yellow is
# inconclusive-but-noteworthy, and grey is the honest "nothing to say".
_VERDICT_COLOUR = {"VERIFIED": _GREEN, "FAILED": _RED, "STALE": _RED,
                   "INDETERMINATE": _YELLOW, "OVERRIDDEN": _TEAL}


def line(repo: Path) -> str:
    """The indicator for `repo`, or "" when there's nothing honest to show."""
    if not state.read_install(repo):
        return ""  # not installed here — never clutter someone else's status bar
    if os.environ.get("TYCHO_STATUS", "").strip().lower() in ("0", "off", "false", "no"):
        return ""  # a global override, for a session where you want it quiet everywhere
    if not state.status_enabled(repo):
        return ""  # toggled off in this repo — the hook still runs, only the badge hides
    beat = state.last_run(repo) or {}
    if not isinstance(beat.get("at"), (int, float)):
        return _paint(_GREY, "[TYCHO]")  # installed, never fired here — bootup, no signal yet
    verdict = beat.get("verdict")
    if verdict is None:
        # A run with no verdict: mid-run (pending) is frost blue "verifying"; a completed run
        # that reached nothing to verify is grey — not a false "working forever".
        return _paint(_FROST if beat.get("pending") else _GREY, "[TYCHO]")
    return _paint(_VERDICT_COLOUR.get(verdict, _GREY), "[TYCHO]")


def _paint(colour: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{colour}{text}{_RESET}"


def repo_of(payload: dict) -> Path:
    """Which repo the bar is being drawn for.

    `workspace.project_dir` is the project root; `cwd` follows the user around it. Prefer
    the root — that's where `.tycho/` lives — and fall back to our own cwd for a human
    running `tycho status` by hand.
    """
    workspace = payload.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("project_dir"), str):
        return Path(workspace["project_dir"])
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) else Path.cwd()


def _stdin_text() -> str:
    """The harness's JSON on stdin as raw text — or "" when a human ran this in a terminal.

    Raw, not parsed, because a *wrapped* status command (composition, TYCHO-47) needs the
    exact same bytes forwarded to it. The isatty check keeps `tycho status` from hanging on
    an interactive read: no payload is coming, and a status command that blocks is worse
    than one that says nothing.
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
    """Run the status command Tycho is composing with, and return its line (TYCHO-47).

    Claude Code's statusLine holds one slot, so to coexist with another status line (a
    third-party badge, a shell prompt) Tycho *becomes* the slot and runs the other command
    itself, forwarding the same payload on stdin. Fail-open to "": a slow or broken
    neighbour must never take Tycho's own segment — or the whole line — down with it.
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
        # This line is UTF-8 (⬤, [TYCHO] colour codes). On Windows, stdout to a pipe
        # defaults to cp1252, where writing it raises UnicodeEncodeError — the crash
        # TYCHO-40 found in `doctor`. Say what encoding we speak instead of dying, and
        # don't assume reconfigure exists (a wrapped stdout may not have it).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = _stdin_text()
    repo = repo_of(_parse(raw))
    if off or on:
        # A human toggling from the terminal (TYCHO-47) — repo is our cwd, not a payload.
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
        # broad catch is the point — this draws in someone's terminal on every
        # render. Empty output is the fail-open, and exit 0 keeps the harness quiet.
        pass
    return 0

"""The verdict palette, shared by the status badge and the log/blame views.

Here rather than in `wire/status.py` because both renderers need it and `views` sits below
`wire` — with the table up there, `archaeology` had to reach back up for it, which is a
cycle between the two packages held open by one lazy import.
"""

from __future__ import annotations

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

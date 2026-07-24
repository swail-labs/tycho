"""Read a test runner's own verdict out of its terminal output. Stdlib `re`, no I/O.

The exit code is the contract Tycho prefers: universal, unambiguous, and impossible for a
runner to get wrong. This module is the fallback for when that contract isn't available —
the shell masked the status (`pytest; echo done`), or the harness never recorded one. In
those cases the runner's own summary line is the best remaining evidence, and it *is*
evidence: the runner reporting on itself, not us inferring from silence.

Kept out of `checks.py` (which owns *which commands are runners*) and out of `events.py`
(which owns *what a harness transcript looks like*) because both need this and neither
should import the other. Codex has read its exit status out of this text since the adapter
landed; this is that knowledge, in one place, for every caller.

Failure is checked before success on purpose: a red summary names both ("1 failed, 76
passed in 1.07s"), so asking "did anything fail?" first is the only order that can't read
a red run as green.

**`.pytest_cache` looks like a better source than this and is not — don't reach for it.**
It is the obvious idea (filesystem truth, immune to truncation and to harnesses that keep
no output), and it is wrong: `v/cache/lastfailed` is not a record of the last run. pytest
seeds it from the previous value and only *removes* an entry when that test runs and
passes, so an entry for a test that was deselected, renamed, or deleted persists forever.
Measured on this repo: `lastfailed` named a test that no longer exists, and survived a
fully green 316-test run untouched — the file isn't even rewritten when nothing changed,
so its mtime can't date the run either. A check trusting it would report FAILED on a green
repo, which is the one failure this program must never have.
"""

from __future__ import annotations

import re

# "1 failed", "3 errors", "=== ERRORS ===". The [1-9] guard matters: a runner that prints
# "0 failed" is reporting success, and a bare `\d+` would read it as the exact opposite.
_FAILURE = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b|=+ ERRORS =+")

# "77 passed in 0.79s" — the count and the duration together. Requiring the duration is
# what keeps a stray "5 passed" in someone's prose from counting as a run that happened.
_SUCCESS = re.compile(r"\b\d+ passed\b.*?\bin \d+(?:\.\d+)?s\b", re.DOTALL)


def outcome(text: str) -> bool | None:
    """True = the runner reported failures, False = it reported success, None = can't tell.

    None is the common case and the honest one: no recognized summary means no verdict,
    never an assumed pass. Mirrors `Event.is_error` so callers can treat the two alike.
    """
    if not text:
        return None
    if _FAILURE.search(text):
        return True
    if _SUCCESS.search(text):
        return False
    return None

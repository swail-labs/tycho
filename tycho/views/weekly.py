"""The weekly line: the one thing Tycho pushes at a user whose agent behaves.

Silence-by-default is the product, and it is also the reason a well-behaved repo can run
Tycho for a month and see nothing — at which point there is no evidence it is still alive
and no reason to keep it. This is the answer that doesn't spend the invariant: one line,
once a week, on the SessionStart channel we already own.

Everything here is read back off `turns.jsonl`. Nothing is inferred and nothing is rounded
up, because a digest that overclaims is the same failure as a green badge over a dead hook.
A week with no turns returns None — "0 turns" is a claim about a week Tycho wasn't watching.

Never raises: it runs from a hook, and every field comes off disk, possibly an older schema.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from .archaeology import _count
from ..store import record

WINDOW = 7 * 86400

# What "caught" means, at both grains. Kept in step with `cli/record.py::_caught` and
# `store/state.py::_RUN_CAUGHT` — an INDETERMINATE is a blind spot, never a save.
_CAUGHT_VERDICTS = ("FAILED", "STALE")
_CAUGHT_STATUSES = ("FAIL", "STALE")


def line(repo: Path, now: float | None = None) -> str | None:
    """One line about the last `WINDOW`, or None when there is nothing to say."""
    now = time.time() if now is None else now
    turns = 0
    verdicts: Counter[str] = Counter()
    offenders: Counter[str] = Counter()
    for row in record.iter_records(repo):
        ended = row.get("ended_at")
        if not isinstance(ended, (int, float)) or isinstance(ended, bool):
            continue
        if ended < now - WINDOW:
            continue
        turns += 1
        verdict = row.get("verdict")
        if verdict in _CAUGHT_VERDICTS:
            verdicts[verdict] += 1
        for check in record._rows(row, "checks"):
            name = check.get("name")
            if check.get("status") in _CAUGHT_STATUSES and isinstance(name, str) and name:
                offenders[name] += 1
    if not turns:
        return None
    return _render(turns, verdicts, offenders)


def _render(turns: int, verdicts: Counter[str], offenders: Counter[str]) -> str:
    head = f"Tycho this week: {_count(turns, 'turn')}"
    caught = sum(verdicts.values())
    if not caught:
        # The quiet week is the one this line exists for: N turns and no catches is the only
        # evidence an invisible tool can offer that it ran at all.
        return f"{head}, nothing caught. `tycho count --ledger` for the record."
    breakdown = ", ".join(f"{verdicts[v]} {v}" for v in _CAUGHT_VERDICTS if verdicts[v])
    return (f"{head}, {caught} caught ({breakdown}){_offender(offenders)}. "
            f"`tycho count --ledger` for the record.")


def _offender(offenders: Counter[str]) -> str:
    """The check that caught the most, named. Ties break on the name rather than on insertion
    order, so two runs over the same week read the same."""
    if not offenders:
        return ""
    name = min(offenders.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return f" — top offender: {name}"

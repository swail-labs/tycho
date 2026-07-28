"""The turn digest: a receipt of what the turn did, not a report card for our checks.

Two surfaces, deliberately different sizes:

- `render()` — the full receipt, on demand (`tycho show`), never unprompted.
- `speaks()` + `brief()` — the unprompted Stop-hook path. `speaks()` returns a `Signal` only
  when the turn is anomalous *for this repo*; silence is the common outcome.

Signals are relative to history, not absolute: a fixed rule ("speak when >5 files changed") is
wrong both on a repo where 20-file turns are normal and one where 2 is a lot, so the norms come
from `.tycho/turns.jsonl`.

**Never raises.** Both surfaces are read from the Stop hook and every field comes off disk
(possibly an older schema), so every accessor tolerates a missing key, wrong type or `None`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from .archaeology import _ago, _count, _trunc
from .model import Stage, Verdict
from .record import _claims, _rows

# How many prior turns the signals read: enough for a norm, short enough that "recent" still
# means recent. ponytail: fixed window, not a decay curve; widen if the norms prove twitchy.
HISTORY = 12

# Verdicts worth interrupting for. VERIFIED/UNSUPPORTED are routine and earn silence.
_ADVERSE = frozenset({Verdict.FAILED.name, Verdict.STALE.name, Verdict.OVERRIDDEN.name})

# Check statuses that mean "this turn is not proven" — the lines worth a headline, worst first.
_UNPROVEN = ("FAIL", "STALE", "INDETERMINATE")

_LADDER = tuple(s.value for s in Stage)


@dataclass(frozen=True)
class Signal:
    """One reason a turn is worth interrupting for.

    `key` is the identity used for decay, deliberately fine-grained: two consecutive FAILED turns
    on the same check are the same news; a FAILED turn on a check that was fine yesterday isn't.
    """

    key: str
    headline: str


# --- the selectivity predicate ------------------------------------------------
#
# Four signals, in headline order; each is rare on a healthy repo:
#   1. `adverse`        — a check says the turn is not proven; names a concrete broken thing.
#   2. `unbacked_claim` — prose says done, the ladder never reached `claim_supported`. Fires on
#                         turns the verdict is happy with, which is the gap it exists to fill.
#   3. `regression`     — a green streak just broke (catches non-adverse INDETERMINATE/UNSUPPORTED).
#   4. `blast_radius`   — far more files than this repo's recent turns; the only scale signal.
#
# Deliberately NOT signals: "VERIFIED again" (wallpaper), "touched a file no recent turn touched"
# (fires constantly on an active repo), and "touched a file no test covers" (that is `tycho
# review`'s question, asked on demand, not shouted at the end of a turn).


def speaks(record: dict, history: Sequence[dict] = (), decay: bool = True) -> Signal | None:
    """The single signal worth interrupting for this turn, or None for silence.

    `history` is this repo's prior turns, **newest first**, excluding `record`. With no history
    the history-relative signals don't fire — inventing a norm would make Tycho loudest exactly
    when it knows least.

    Novelty decay: a condition that fired on the last `_DECAY_AFTER` turns is suppressed even
    though still true. The relay is unaffected and keeps pushing the agent at a standing failure;
    `decay=False` is the human opting out (see `hook._digest_output`).
    ponytail: ceiling is a stuck repo going quiet after two turns — intended, and `tycho show`
    is always there.
    """
    fired = signals(record, history)
    if not fired:
        return None
    if not decay:
        return fired[0]
    fresh = [s for s in fired if s.key not in _recently_said(history)]
    return fresh[0] if fresh else None


# How many consecutive prior turns must have carried a signal before it stops being news.
# ponytail: two is the smallest number that can tell "again" from "still"; raise it if
# users report the digest going quiet too eagerly.
_DECAY_AFTER = 2


def _recently_said(history: Sequence[dict]) -> frozenset[str]:
    """Signal keys that fired on *every* one of the last `_DECAY_AFTER` turns.

    Intersection, not union: a signal that fired once two turns ago is still news; one that has
    been true continuously is not. Each historical turn is scored against the turns *before* it.
    """
    if len(history) < _DECAY_AFTER:
        return frozenset()
    said = [
        {s.key for s in signals(rec, history[i + 1:])}
        for i, rec in enumerate(history[:_DECAY_AFTER])
    ]
    return frozenset(said[0].intersection(*said[1:]))


def signals(record: dict, history: Sequence[dict] = ()) -> tuple[Signal, ...]:
    """Every signal this turn raises, most headline-worthy first. Pure; never raises."""
    found = (
        _adverse(record),
        _unbacked_claim(record),
        _regression(record, history),
        _blast_radius(record, history),
    )
    return tuple(s for s in found if s is not None)


def _adverse(record: dict) -> Signal | None:
    """FAILED / STALE / OVERRIDDEN — a check we ran says the turn is not proven.

    The headline is the worst *unproven check*, not the verdict word: "test_freshness — app.py
    edited after the last run" says where to look; "FAILED" says go find out. Worst, not first,
    because `checks` is in registry order — a standing STALE would otherwise headline over the
    FAIL that actually failed the turn.

    The decay key carries the verdict and the *whole* set of unproven checks, so a turn that gets
    worse (or an OVERRIDDEN run that becomes a real FAILED) mints a new key and is news again.
    """
    verdict = _text(record.get("verdict"))
    if verdict not in _ADVERSE:
        return None
    bad = _unproven_checks(record)
    key = f"adverse:{verdict}:" + ",".join(sorted(f"{n}={s}" for n, s, _ in bad))
    if bad:
        name, _, evidence = min(bad, key=lambda c: _UNPROVEN.index(c[1]))
        return Signal(key, f"{verdict} — {name}: {evidence}")
    # OVERRIDDEN can reach here with everything PASS, so there is nothing to name.
    return Signal(key, f"{verdict} — no check could confirm this turn")


# Prose that asserts the work is finished. Only ever used to ask a question, never to fail a
# turn, so an over-broad match costs a line and never a false verdict.
# ponytail: a word list, not NLP — an LLM would violate the no-network/no-model invariant.
# Calibration knob: add rows.
_DONE_PROSE = re.compile(
    r"(?i)\b(?:"
    r"all (?:the )?tests?(?: now)? pass|tests?(?: now)? pass|"
    r"(?:it |that |everything )?(?:now )?works?(?: now)?|"
    r"(?:is |are |it's |now )?(?:fixed|done|complete|completed|resolved|working)|"
    r"ready to (?:merge|ship|review)|should (?:work|pass)(?: now)?|"
    r"successfully (?:fixed|added|implemented|migrated|updated)"
    r")\b"
)


def _unbacked_claim(record: dict) -> Signal | None:
    """The prose says finished; the acceptance ladder never reached `claim_supported`.

    Requires *both* halves: a turn that stops at `artifact_changed` without claiming anything is
    normal work in progress, and interrupting for it would be every-turn wallpaper.
    """
    stage = _text(record.get("stage"))
    if stage == Stage.CLAIM_SUPPORTED.value:
        return None
    said = next((c for c in _claims(record) if _DONE_PROSE.search(c)), None)
    if said is None:
        return None
    rung = stage or "nothing recorded"
    return Signal(
        f"unbacked_claim:{rung}",
        f'said "{_trunc(said, 48)}" — evidence stopped at {rung}',
    )


# How many proven turns in a row make the next non-proven one news. Below three, a green run
# isn't a streak — it's a coincidence. ponytail: calibration knob.
_GREEN_STREAK = 3


def _regression(record: dict, history: Sequence[dict]) -> Signal | None:
    """This repo was proving its turns, and now it isn't.

    INDETERMINATE/UNSUPPORTED are the routine "we couldn't tell" outcome and earn silence —
    except the first time they interrupt a run of proven turns, the shape of a newly-broken
    test command.
    """
    if _text(record.get("verdict")) == Verdict.VERIFIED.name:
        return None
    green = 0
    for prior in history:
        if _text(prior.get("verdict")) != Verdict.VERIFIED.name:
            break
        green += 1
    if green < _GREEN_STREAK:
        return None
    return Signal("regression", f"first unproven turn after {green} proven ones")


# A turn must be at least this many files AND this multiple of the repo's recent median before
# scale is worth mentioning. Both, because the multiple alone screams at 3 files on a repo that
# usually touches 1, and the floor alone screams at every turn in a repo that edits 6 at a time.
# ponytail: two flat thresholds beat a z-score nobody can predict the behaviour of.
_BLAST_FLOOR = 5
_BLAST_FACTOR = 3
# Below this many prior turns there is no "recent norm", only a small sample pretending to be one.
_BLAST_MIN_HISTORY = 4


def _blast_radius(record: dict, history: Sequence[dict]) -> Signal | None:
    """Far more files touched than this repo's recent turns touch.

    Not a failure — a "look at this one before you move on". Median, not mean: one 40-file
    refactor last week must not raise the bar for the next month.
    """
    n = len(_files(record))
    prior = [len(_files(h)) for h in history]
    if len(prior) < _BLAST_MIN_HISTORY:
        return None
    norm = median(prior)
    if n < max(_BLAST_FLOOR, _BLAST_FACTOR * norm):
        return None
    return Signal(
        "blast_radius",
        f"{n} files touched — recent turns touch {_num(norm)}",
    )


# --- rendering ----------------------------------------------------------------


def brief(record: dict, signal: Signal | None = None) -> str:
    """The unprompted Stop-hook digest: four lines, anomaly first.

    Why we spoke, then the ladder, then the turn's shape, then a pointer to the full receipt.
    Evidence nobody reads is evidence that doesn't exist — hence the four-line budget.
    """
    headline = signal.headline if signal else _text(record.get("verdict")) or "turn recorded"
    lines = [
        f"🔍 Tycho: {headline}",
        f"   {_ladder(record)}",
    ]
    facts = _facts(record)
    if facts:
        lines.append(f"   {facts}")
    lines.append("   `tycho show` for the full receipt")
    return "\n".join(lines)


def render(record: dict, now: float | None = None) -> str:
    """The full digest for one turn record — the on-demand view (`tycho show`).

    Ordered the way a developer reconstructs a turn: how far it got, what it changed, what it
    ran, what it said, what is still unproven.

    The age is in the header because `tycho show` falls back to the newest record there is: on a
    turn the hook wrote nothing for, an undated receipt reads as this turn's.
    """
    turn = _text(record.get("id")) or "?"
    verdict = _text(record.get("verdict")) or "?"
    age = _ago(record.get("ended_at"), time.time() if now is None else now)
    lines = [f"🔍 Tycho: turn {turn} · {verdict} · {age}", f"   ladder   {_ladder(record)}"]
    files = _files(record)
    if files:
        shown = ", ".join(f"{f['path']}{'*' if f['kind'] == 'create' else ''}" for f in files[:12])
        extra = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
        lines.append(_wrap("changed", f"{_count(len(files), 'file')}: {shown}{extra}"))
    for i, cmd in enumerate(_commands(record)):
        lines.append(_wrap("ran" if i == 0 else "", f"{cmd['cmd']} → {cmd['outcome']}"))
    for i, claim in enumerate(_claims(record)[:4]):
        lines.append(_wrap("claimed" if i == 0 else "", f'"{_trunc(claim, 100)}"'))
    unproven = _unproven_checks(record)
    for i, (name, _, evidence) in enumerate(unproven):
        lines.append(_wrap("open" if i == 0 else "", f"{name} — {evidence}"))
    if not unproven and verdict == Verdict.VERIFIED.name:
        lines.append(_wrap("open", "nothing — every check that applied passed"))
    return "\n".join(lines)


def _ladder(record: dict) -> str:
    """The acceptance ladder with every rung shown, reached ones ticked.

    All four rungs always — the unreached ones are the point. `·` rather than `✗` because an
    unreached rung is a gap in the evidence, not a failure.

    It does not simply tick everything up to `stage`: `record.stage_of` returns the highest rung
    that *matches* by priority, not a chain, so a turn that wrote a file but ran nothing is
    `artifact_changed` and ticking `executed` under it would assert a test run that never
    happened. The two independently confirmable rungs are re-checked, and that can only remove
    a tick — including `claim_supported`, which needs one of them under it or the chain it draws
    is one nothing ran and nothing changed.
    """
    try:
        reached = _LADDER.index(_text(record.get("stage")))
    except ValueError:
        reached = -1  # unknown/missing stage (older schema) — show the ladder, tick nothing
    ticked = [i <= reached for i in range(len(_LADDER))]
    ticked[1] = ticked[1] and any(c["runner"] for c in _commands(record))
    ticked[2] = ticked[2] and bool(_files(record))
    ticked[3] = ticked[3] and (ticked[1] or ticked[2])
    return "  ".join(f"{'✓' if ticked[i] else '·'} {r}" for i, r in enumerate(_LADDER))


def _facts(record: dict) -> str:
    """One line of turn shape: files touched, then commands and what they returned.

    Runners first, so `pytest -q failed` survives the truncation a turn with 30 `ls` calls
    would otherwise cause.
    """
    parts = []
    files = _files(record)
    if files:
        parts.append(_count(len(files), "file"))
    commands = sorted(_commands(record), key=lambda c: not c["runner"])
    parts.extend(f"{_trunc(c['cmd'], 40)} {c['outcome']}" for c in commands[:2])
    if len(commands) > 2:
        parts.append(f"+{len(commands) - 2} more")
    return " · ".join(parts)


def _wrap(label: str, text: str) -> str:
    """A digest body line: an 8-column label gutter, blank on continuation lines."""
    return f"   {label:<8} {text}"


# --- safe accessors -----------------------------------------------------------
#
# A malformed row renders a shorter digest, never a traceback in the Stop hook.


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _files(record: dict) -> list[dict]:
    return [
        {"path": _text(r.get("path")), "kind": _text(r.get("kind")) or "edit"}
        for r in _rows(record, "files")
        if _text(r.get("path"))
    ]


def _commands(record: dict) -> list[dict]:
    return [
        {
            "cmd": _text(r.get("cmd")),
            "outcome": _text(r.get("outcome")) or "unknown",
            "runner": bool(r.get("runner")),
        }
        for r in _rows(record, "commands")
        if _text(r.get("cmd"))
    ]


def _unproven_checks(record: dict) -> list[tuple[str, str, str]]:
    """(name, status, evidence) for every check that didn't prove out, in the order they ran."""
    return [
        (
            _text(r.get("name")) or "check",
            _text(r.get("status")),
            _text(r.get("evidence")) or "no evidence recorded",
        )
        for r in _rows(record, "checks")
        if _text(r.get("status")) in _UNPROVEN
    ]


def _num(value: float) -> str:
    """A median printed the way a person would say it: `2`, not `2.0`; `1.5` stays `1.5`."""
    return f"{value:g}"

"""The turn digest: a receipt of what the turn did, not a report card for our checks.

Strategy §6.1/§9.1. `report.render` answers *"did it lie?"* — a question asked rarely, and
answered VERIFIED nearly every time, so by turn 20 nobody reads it. The digest answers the
questions asked after every turn: what changed, what ran, what was claimed, what is still
unverified. The verdict becomes one field, not the headline.

**Selectivity is the feature, not a nicety** (§11.1). A fixed-shape summary after every turn
is wallpaper by day three; the doc's target is *10x less output, 10x more value per message*.
So there are two surfaces here and they are deliberately different sizes:

- `render()` — the full receipt. Always available on demand (`tycho show`), never unprompted.
- `speaks()` + `brief()` — the unprompted Stop-hook path. `speaks()` returns a `Signal` only
  when this turn is genuinely anomalous *for this repo*, and `brief()` renders it in four
  lines. Silence is the default and by far the most common outcome.

**Why the signals are relative to history, not absolute.** A fixed rule ("speak when >5 files
changed") is wrong on both a repo where 20-file turns are normal and one where 2 is a lot.
`.tycho/turns.jsonl` is this repo's own record, so the norms come from it — which is what lets
the digest stay quiet where something is routine and speak where it isn't.

**Never raises.** Both surfaces are read from the Stop hook, and every field here comes off
disk (possibly written by an older schema), so every accessor tolerates a missing key, a wrong
type, or a `None`. A digest we can't compute is a digest we don't print — never an exception.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from .model import Stage, Verdict

# How many prior turns the signals read. Enough to have a norm for this repo, short enough
# that "recent" still means recent — a month-old blast radius is not this repo's habit any
# more. ponytail: fixed window, not a decay curve; widen only if the norms prove twitchy.
HISTORY = 12

# Verdicts worth interrupting for. VERIFIED/UNSUPPORTED are the routine outcome — the whole
# point of §11.1 is that they earn silence.
_ADVERSE = frozenset({Verdict.FAILED.name, Verdict.STALE.name, Verdict.OVERRIDDEN.name})

# Check statuses that mean "this turn is not proven" — the lines worth a headline.
_UNPROVEN = frozenset({"FAIL", "STALE", "INDETERMINATE"})

_LADDER = tuple(s.value for s in Stage)


@dataclass(frozen=True)
class Signal:
    """One reason a turn is worth interrupting for.

    `key` is the signal's *identity* for decay purposes, which is deliberately finer than
    `name`: two consecutive FAILED turns on the same check are the same news, but a FAILED
    turn on a check that was fine yesterday is new news. See `speaks`.
    """

    name: str
    key: str
    headline: str


# --- the selectivity predicate ------------------------------------------------
#
# Four signals, in headline order. They were picked over the alternatives because each one
# answers a question a developer actually has, and each one is *rare* on a healthy repo:
#
# 1. `adverse`      — a check we ran says the turn is not proven. The one signal that needs no
#                     justification, and the only one naming a concrete broken thing, so it
#                     leads even when others also fire.
# 2. `unbacked_claim` — the prose says done, the acceptance ladder never reached
#                     `claim_supported`. This is §4's "code written, tests never ran", the case
#                     users described unprompted and the highest-value inference Tycho can make.
#                     It fires on turns the *verdict* is happy with, which is exactly the gap
#                     the verdict-shaped output left open.
# 3. `regression`   — a green streak just broke. Catches the non-VERIFIED verdicts that aren't
#                     adverse (INDETERMINATE/UNSUPPORTED): normally noise, real news the first
#                     time they follow a run of proven turns.
# 4. `blast_radius` — this turn touched far more files than this repo's recent turns do. Not a
#                     failure — a "look at this one" — and the only signal about *scale*.
#
# Deliberately NOT signals: "VERIFIED again" (the wallpaper §11.1 is about), "touched a file no
# recent turn touched" (fires constantly on any active repo), and "touched a file no test
# covers" (that is `tycho review`'s question, asked on demand inside a review, not shouted at
# the end of a turn).


def speaks(record: dict, history: Sequence[dict] = (), decay: bool = True) -> Signal | None:
    """The single signal worth interrupting for this turn, or None for silence.

    `history` is this repo's prior turns, **newest first**, not including `record` (what
    `record.read(repo, limit=HISTORY)` returns *before* the current turn is appended).
    With no history the history-relative signals simply don't fire — a first turn on a fresh
    repo has no norm to deviate from, and inventing one would make Tycho loudest exactly when
    it knows least.

    **Novelty decay.** A condition that fired on the last `_DECAY_AFTER` turns is no longer
    news, so it is suppressed even though it is still true. The relay is unaffected — it keeps
    pushing the agent at a standing failure whether or not the human is told again (see
    `hook._relay_output`). `decay=False` turns the suppression off for a caller who has been
    told the human wants every one of these (the relay opt-in; see `hook._digest_output`).
    ponytail: the ceiling is that a stuck repo goes quiet after two turns; that is the intended
    trade (§11.1), and `tycho show` is always there.
    """
    fired = signals(record, history)
    if not fired:
        return None
    if not decay:
        return fired[0]
    fresh = [s for s in fired if s.key not in _recently_said(history)]
    return fresh[0] if fresh else None


# How many consecutive prior turns must have carried a signal before it stops being news.
# ponytail: two is the smallest number that can tell "again" from "still"; raise it with
# TYCHO-style calibration if users report the digest going quiet too eagerly.
_DECAY_AFTER = 2


def _recently_said(history: Sequence[dict]) -> frozenset[str]:
    """Signal keys that fired on *every* one of the last `_DECAY_AFTER` turns.

    Intersection, not union: a signal that fired once two turns ago is still news today; one
    that has been true continuously is not. Each historical turn is scored against the turns
    *before* it, so its signals are computed exactly as they were when it was live.
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

    The headline is the first *unproven check*, not the verdict word: "test_freshness — app.py
    edited after the last run" tells a developer where to look; "FAILED" tells them to go find
    out. The verdict still rides along, because it is what `tycho verify` and the docs name.
    """
    verdict = _text(record.get("verdict"))
    if verdict not in _ADVERSE:
        return None
    bad = _unproven_checks(record)
    if bad:
        name, evidence = bad[0]
        return Signal("adverse", f"adverse:{name}", f"{verdict} — {name}: {evidence}")
    # OVERRIDDEN can reach here with everything PASS (the agent set the check aside and the
    # rest were fine), so there is nothing to name — say so rather than printing a bare word.
    return Signal(
        "adverse", f"adverse:{verdict}", f"{verdict} — no check could confirm this turn"
    )


# Prose that asserts the work is finished. Matched against the agent's own messages, and only
# ever used to *ask a question* ("you said done — the evidence stopped here"), never to fail a
# turn, so an over-broad match costs a line and never a false verdict.
# ponytail: a word list, not NLP — an LLM here would violate the no-network/no-model invariant,
# and the phrasing agents use for "done" is a genuinely small set. Calibration knob: add rows.
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

    §4's "code written, tests never ran", and the reason the ladder is the digest's spine: the
    verdict can be perfectly happy with a turn whose evidence chain stopped two rungs short.
    Requires *both* halves — a turn that quietly stops at `artifact_changed` without claiming
    anything is normal work in progress, and interrupting for it would be exactly the
    every-turn wallpaper §11.1 warns about.
    """
    stage = _text(record.get("stage"))
    if stage == Stage.CLAIM_SUPPORTED.value:
        return None
    said = next((c for c in _claims(record) if _DONE_PROSE.search(c)), None)
    if said is None:
        return None
    rung = stage or "nothing recorded"
    return Signal(
        "unbacked_claim",
        f"unbacked_claim:{rung}",
        f'said "{_snip(said, 48)}" — evidence stopped at {rung}',
    )


# How many proven turns in a row make the next non-proven one news. Below three, a green run
# isn't a streak — it's a coincidence. ponytail: calibration knob.
_GREEN_STREAK = 3


def _regression(record: dict, history: Sequence[dict]) -> Signal | None:
    """This repo was proving its turns, and now it isn't.

    Catches what `_adverse` deliberately doesn't: INDETERMINATE and UNSUPPORTED are the routine
    "we couldn't tell" outcome and earn silence — *except* the first time they interrupt a run
    of genuinely proven turns, which is the shape of a newly-broken test command.
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
    return Signal("regression", "regression", f"first unproven turn after {green} proven ones")


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

    Not a failure and not phrased as one — it is the "look at this one before you move on"
    signal, and the only thing here that is about *size* rather than evidence. Median, not
    mean: one 40-file refactor last week must not raise the bar for the next month.
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
        "blast_radius",
        f"{n} files touched — recent turns touch {_num(norm)}",
    )


# --- rendering ----------------------------------------------------------------


def brief(record: dict, signal: Signal | None = None) -> str:
    """The unprompted Stop-hook digest: four lines, anomaly first (§6.1's output budget).

    Line 1 is *why we spoke* — never "VERIFIED again". Line 2 is the ladder, so the reader sees
    how far the evidence chain got without reading anything else. Line 3 is the turn's shape
    (files, what ran). Line 4 points at the full receipt. §4's warning is the constraint:
    evidence nobody reads is evidence that doesn't exist.
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


def render(record: dict) -> str:
    """The full digest for one turn record — the on-demand view (`tycho show`).

    A receipt, in the order a developer reconstructs a turn: how far it got, what it changed,
    what it ran and what that returned, what it said about it, and what is still unproven. The
    verdict is one field on the header line, not the headline.
    """
    turn = _text(record.get("id")) or "?"
    verdict = _text(record.get("verdict")) or "?"
    lines = [f"🔍 Tycho: turn {turn} · {verdict}", f"   ladder   {_ladder(record)}"]
    files = _files(record)
    if files:
        shown = ", ".join(f"{f['path']}{'*' if f['kind'] == 'create' else ''}" for f in files[:12])
        extra = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
        lines.append(_wrap("changed", f"{_count(len(files), 'file')}: {shown}{extra}"))
    for i, cmd in enumerate(_commands(record)):
        lines.append(_wrap("ran" if i == 0 else "", f"{cmd['cmd']} → {cmd['outcome']}"))
    for i, claim in enumerate(_claims(record)[:4]):
        lines.append(_wrap("claimed" if i == 0 else "", f'"{_snip(claim, 100)}"'))
    unproven = _unproven_checks(record)
    for i, (name, evidence) in enumerate(unproven):
        lines.append(_wrap("open" if i == 0 else "", f"{name} — {evidence}"))
    if not unproven and verdict == Verdict.VERIFIED.name:
        lines.append(_wrap("open", "nothing — every check that applied passed"))
    return "\n".join(lines)


def _ladder(record: dict) -> str:
    """The acceptance ladder with every rung shown, reached ones ticked (strategy §6.4).

    All four rungs, always — the unreached ones are the point. "✓ attempted ✓ executed ·
    artifact_changed · claim_supported" says *the tests ran but the files never landed* at a
    glance, which is strictly more than a verdict word can say. `·` rather than `✗` because an
    unreached rung is a gap in the evidence, not a failure.

    **Why this doesn't just tick everything up to `stage`.** `record.stage_of` returns the
    highest rung that *matches*, choosing by descending priority — it is not a chain. A turn
    that wrote a file but ran nothing is `artifact_changed`, and ticking `executed` under it
    would have the digest assert a test run that never happened, which is the exact class of
    claim Tycho exists to disprove. So the two rungs the record can independently confirm are
    confirmed against it, and a correction here can only ever *remove* a tick.
    """
    try:
        reached = _LADDER.index(_text(record.get("stage")))
    except ValueError:
        reached = -1  # unknown/missing stage (older schema) — show the ladder, tick nothing
    ticked = [i <= reached for i in range(len(_LADDER))]
    ticked[1] = ticked[1] and any(c["runner"] for c in _commands(record))
    ticked[2] = ticked[2] and bool(_files(record))
    return "  ".join(f"{'✓' if ticked[i] else '·'} {r}" for i, r in enumerate(_LADDER))


def _facts(record: dict) -> str:
    """One line of turn shape: files touched, then the commands that ran and what they returned.

    Runners first — `pytest -q failed` is the fact a developer wants off this line, and it must
    survive the truncation that a turn with 30 `ls` calls would otherwise cause.
    """
    parts = []
    files = _files(record)
    if files:
        parts.append(_count(len(files), "file"))
    commands = sorted(_commands(record), key=lambda c: not c["runner"])
    parts.extend(f"{_snip(c['cmd'], 40)} {c['outcome']}" for c in commands[:2])
    if len(commands) > 2:
        parts.append(f"+{len(commands) - 2} more")
    return " · ".join(parts)


def _wrap(label: str, text: str) -> str:
    """A digest body line: an 8-column label gutter, blank on continuation lines."""
    return f"   {label:<8} {text}"


# --- safe accessors -----------------------------------------------------------
#
# Every one of these reads a record off disk that may have been written by an older schema, by
# a crashed append, or (via `tycho show <id>`) hand-edited. They coerce rather than validate:
# a malformed row renders a shorter digest, never a traceback in the Stop hook.


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _rows(record: dict, key: str) -> list[dict]:
    value = record.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


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


def _claims(record: dict) -> list[str]:
    value = record.get("claims")
    if not isinstance(value, list):
        return []
    return [c.strip() for c in value if isinstance(c, str) and c.strip()]


def _unproven_checks(record: dict) -> list[tuple[str, str]]:
    """(name, evidence) for every check that didn't prove out, in the order they were run."""
    return [
        (_text(r.get("name")) or "check", _text(r.get("evidence")) or "no evidence recorded")
        for r in _rows(record, "checks")
        if _text(r.get("status")) in _UNPROVEN
    ]


def _snip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _num(value: float) -> str:
    """A median printed the way a person would say it: `2`, not `2.0`; `1.5` stays `1.5`."""
    return f"{value:g}"

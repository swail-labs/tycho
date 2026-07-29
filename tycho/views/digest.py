"""The turn digest: a receipt of what the turn did, not a report card for our checks.

Two surfaces, deliberately different sizes:

- `render()` — the full receipt, on demand (`tycho show`), never unprompted.
- `speaks()` + `brief()` — the unprompted Stop-hook path. `speaks()` returns a `Signal` only
  when the turn is anomalous *for this repo*; silence is the common outcome.

Signals are relative to history: a fixed rule ("speak when >5 files changed") is wrong both on
a repo where 20-file turns are normal and one where 2 is a lot, so the norms come from
`.tycho/turns.jsonl`. Never raises — every field comes off disk, possibly an older schema.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from .archaeology import _ago, _count, _trunc
from ..model import Stage, Verdict
from ..store.record import _claims, _rows

# ponytail: fixed window, not a decay curve; widen if the norms prove twitchy.
HISTORY = 12

# Verdicts worth interrupting for. VERIFIED/UNSUPPORTED are routine and earn silence.
_ADVERSE = frozenset({Verdict.FAILED.name, Verdict.STALE.name, Verdict.OVERRIDDEN.name})

# Statuses meaning "not proven", worst first.
_UNPROVEN = ("FAIL", "STALE", "INDETERMINATE")

_LADDER = tuple(s.value for s in Stage)


@dataclass(frozen=True)
class Signal:
    """One reason a turn is worth interrupting for. `key` is the decay identity, fine-grained:
    two FAILED turns on the same check are the same news, on a different check they aren't."""

    key: str
    headline: str


# --- the selectivity predicate ------------------------------------------------
#
# Four signals, in headline order, each rare on a healthy repo: `adverse`, `unbacked_claim`,
# `regression`, `blast_radius`. Deliberately NOT signals: "VERIFIED again" (wallpaper), a file
# no recent turn touched (constant on an active repo), and a file no test covers (that is
# `tycho review`'s question, asked on demand).


def speaks(record: dict, history: Sequence[dict] = (), decay: bool = True) -> Signal | None:
    """The single signal worth interrupting for this turn, or None for silence.

    `history` is this repo's prior turns, newest first, excluding `record`. With none, the
    history-relative signals stay quiet — inventing a norm would make Tycho loudest when it
    knows least. A condition that fired on the last `_DECAY_AFTER` turns is suppressed even
    though still true; the relay is unaffected and keeps pushing at a standing failure.
    """
    fired = signals(record, history)
    if not fired:
        return None
    if not decay:
        return fired[0]
    fresh = [s for s in fired if s.key not in _recently_said(history)]
    return fresh[0] if fresh else None


# ponytail: two is the smallest number that can tell "again" from "still".
_DECAY_AFTER = 2


def _recently_said(history: Sequence[dict]) -> frozenset[str]:
    """Signal keys that fired on *every* one of the last `_DECAY_AFTER` turns. Intersection,
    not union: one that fired once two turns ago is still news, one that never stopped isn't."""
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
    """FAILED / STALE / OVERRIDDEN — a check says the turn is not proven.

    The headline names the worst unproven *check*, not the verdict word: "FAILED" says go find
    out, the check says where to look. Worst, not first — `checks` is in registry order, so a
    standing STALE would headline over the FAIL that actually sank the turn. The decay key
    carries the whole unproven set, so a turn that gets worse is news again.
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


# Prose asserting the work is finished. Only ever asks a question, never fails a turn, so an
# over-broad match costs a line. ponytail: a word list — an LLM would break the no-model
# invariant. Calibration knob: add rows.
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
    """Prose says finished; the ladder never reached `claim_supported`. Both halves required —
    stopping at `artifact_changed` without claiming anything is normal work in progress."""
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


# Below three, a green run isn't a streak — it's a coincidence. ponytail: calibration knob.
_GREEN_STREAK = 3


def _regression(record: dict, history: Sequence[dict]) -> Signal | None:
    """This repo was proving its turns, and now it isn't. INDETERMINATE/UNSUPPORTED earn
    silence except the first time they break a run of proven ones."""
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


# Both a floor AND a multiple of the median: the multiple alone screams at 3 files on a repo
# that usually touches 1, the floor alone at every turn on a repo that edits 6 at a time.
# ponytail: two flat thresholds beat a z-score nobody can predict.
_BLAST_FLOOR = 5
_BLAST_FACTOR = 3
_BLAST_MIN_HISTORY = 4


def _blast_radius(record: dict, history: Sequence[dict]) -> Signal | None:
    """Far more files touched than this repo's recent turns touch — not a failure, a "look at
    this one". Median, not mean: one 40-file refactor must not raise the bar for a month."""
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
    """The unprompted Stop-hook digest: why we spoke, the ladder, the turn's shape, a pointer
    to the full receipt. Evidence nobody reads doesn't exist — hence the four-line budget."""
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


def render(record: dict, now: float | None = None, share: bool = False) -> str:
    """The full digest for one turn record (`tycho show`), ordered the way a developer
    reconstructs a turn: how far it got, what it changed, ran, said, what is still unproven.

    The age is in the header because `tycho show` falls back to the newest record there is,
    and an undated receipt reads as this turn's.

    `share=True` is the paste-able variant (`tycho show --share`). A catch is the one thing
    anyone screenshots, and the two jobs pull against each other: the claim beside the
    evidence that contradicts it *is* the story, so it stays; the turn id, the age and the
    directory tree are this machine's, not the story's, so they go. The footer is there
    because the receipt lands where nobody has heard of an acceptance ladder.
    """
    turn = _text(record.get("id")) or "?"
    verdict = _text(record.get("verdict")) or "?"
    age = _ago(record.get("ended_at"), time.time() if now is None else now)
    header = f"🔍 Tycho: {verdict}" if share else f"🔍 Tycho: turn {turn} · {verdict} · {age}"
    lines = [header, f"   ladder   {_ladder(record)}"]
    files = _files(record)
    if files:
        shown = ", ".join(
            f"{_path(f['path'], share)}{'*' if f['kind'] == 'create' else ''}" for f in files[:12]
        )
        extra = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
        lines.append(_wrap("changed", f"{_count(len(files), 'file')}: {shown}{extra}"))
    shown_cmds, dropped = _shown_commands(_commands(record))
    for i, cmd in enumerate(shown_cmds):
        ran = f"{_trunc(_shared(cmd['cmd'], share), 96)} → {cmd['outcome']}"
        lines.append(_wrap("ran" if i == 0 else "", ran))
    if dropped:
        lines.append(_wrap("", f"(+{dropped} more)"))
    for i, claim in enumerate(_claims(record)[:4]):
        lines.append(_wrap("claimed" if i == 0 else "", f'"{_trunc(_shared(claim, share), 100)}"'))
    unproven = _unproven_checks(record)
    for i, (name, _, evidence) in enumerate(unproven):
        lines.append(_wrap("open" if i == 0 else "", f"{name} — {_shared(evidence, share)}"))
    if not unproven and verdict == Verdict.VERIFIED.name:
        lines.append(_wrap("open", "nothing — every check that applied passed"))
    if share:
        lines.append(f"   {_TAGLINE}")
    return "\n".join(lines)


# One line, so a stranger reading a screenshot knows what they're looking at and that no
# model graded it. The "no LLM" half is the claim nobody else in this space can make.
_TAGLINE = "tycho — deterministic, offline check of what the agent actually did. No LLM."


def _path(path: str, share: bool) -> str:
    """A path as the receipt should carry it. `tycho show` gives the whole thing, because its
    reader is the person who has to go and fix it. A shared receipt gives the basename:
    `pricing.py` tells the same story as `src/billing/internal/pricing.py` without publishing
    a private tree to a timeline."""
    return path.rsplit("/", 1)[-1] if share else path


# Every path-shaped run, so `src/billing/pricing.py` in a check's own evidence collapses the
# same way the `changed` line does. Without this the receipt shortened the path on one line
# and published it in full two lines down — the promise on the flag, broken inside one screen.
#
# ponytail: one regex over the whole line, not a path parser. It also shortens the path part
# of a URL, which on a receipt meant for a timeline is the same trade, not a bug.
_PATHY = re.compile(r"[A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)+")


def _shared(text: str, share: bool) -> str:
    """Free text as a shared receipt should carry it: paths down to their last segment."""
    if not share:
        return text
    return _PATHY.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text)


def _ladder(record: dict) -> str:
    """The ladder with every rung shown, reached ones ticked. All four always — the unreached
    ones are the point, and `·` not `✗` because a gap in evidence isn't a failure.

    Not simply everything up to `stage`: `record.stage_of` returns the highest *matching* rung,
    not a chain, so a turn that wrote a file but ran nothing is `artifact_changed` and ticking
    `executed` under it would assert a run that never happened. The two independently
    confirmable rungs are re-checked, which can only remove a tick.
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
    """One line of turn shape. Runners first, so `pytest -q failed` survives the truncation a
    turn with 30 `ls` calls would cause."""
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
# A malformed row renders a shorter digest, never a traceback in the Stop hook.


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# A receipt nobody finishes reading is a receipt nobody reads. One turn here rendered 38
# commands, one of them a heredoc that put a whole test function in the digest.
_MAX_COMMANDS = 12


def _shown_commands(commands: list[dict]) -> tuple[list[dict], int]:
    """The commands worth printing, in order, and how many were left out.

    Every runner survives the cap no matter where it ran: "did the suite run, and did it
    pass" is the line the receipt exists for, and dropping it to fit a `grep` from earlier in
    the turn would cut the evidence and keep the noise. The rest is filled from the most
    recent, which is the part someone reading a receipt is asking about.
    """
    runners = {i for i, c in enumerate(commands) if c["runner"]}
    room = max(0, _MAX_COMMANDS - len(runners))
    rest = [i for i in range(len(commands)) if i not in runners]
    keep = runners | set(rest[-room:] if room else [])
    return [commands[i] for i in sorted(keep)], len(commands) - len(keep)


def _files(record: dict) -> list[dict]:
    """One entry per *path*, not per edit — the record holds one row per write, so a file
    edited four times is four rows.

    Deduping here rather than at each caller because all four read this: the receipt said
    "4 files" and listed the same path three times, the ladder and summary counted the same
    way, and `_unusual_breadth` compared an edit count against a file count — so re-editing
    one file looked like a wide turn and spoke up about it. A create that was later edited
    is still a create.
    """
    seen: dict[str, dict] = {}
    for row in _rows(record, "files"):
        path = _text(row.get("path"))
        if not path:
            continue
        kind = _text(row.get("kind")) or "edit"
        if path in seen:
            if kind == "create":
                seen[path]["kind"] = "create"
        else:
            seen[path] = {"path": path, "kind": kind}
    return list(seen.values())


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
    """`2`, not `2.0`; `1.5` stays `1.5`."""
    return f"{value:g}"

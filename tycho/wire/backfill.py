"""`tycho backfill` — seed the record from transcripts written before Tycho was installed.

Everything the record buys — `blame`, `log`, `review`, the anomaly norms — pays off in week
three, and a fresh install has an empty ledger. But the history is already on disk: Claude
Code keeps every session under `~/.claude/projects/<repo>/`, going back as far as the user's
retention. This reads those, cuts each transcript into the turns that produced it, and writes
one record per turn — so `tycho blame` answers on the day it is installed.

**A backfilled turn carries no verdict, and that is not a limitation being apologized for.**
Six of the nine checks are functions of state that no longer exists: `file_state` asks whether
an edited file is on disk *now*, `test_freshness` compares an mtime *now* against a run three
weeks ago, the tamper checks diff a baseline against the file's current text. Replaying them
against today's tree does not reconstruct a past verdict — it invents one, and every invented
verdict lands in a file whose whole value is that it is a record. So a backfilled row states
what the transcript itself says (files touched, commands run and what they returned, the
agent's own claims, when) and stops:

    verdict     "UNVERIFIED"   — never verified, distinct from every verdict Tycho renders
    checks      []             — nothing ran, so nothing is claimed
    backfilled  true           — so the decay ledger can exclude it from every rate

That last field is load-bearing. Catch rate and blind rate are the numbers §7 of the strategy
turns into a published series; diluting them with turns nobody checked would corrupt exactly
the measurement the record exists to make.

ponytail: replaying the transcript-pure checks (`command_execution`, `tool_call_provenance`)
would yield real historical verdicts for a subset. Deliberately not done — it needs a
per-check "reads no filesystem" flag that a later edit to a check silently invalidates, and a
wrong verdict in the record costs more than a missing one. Revisit if `blame` proves it wants
more than facts.
"""

from __future__ import annotations

from pathlib import Path

from ..model import Attribution, GitSnapshot, Session
from ..read import events as events_mod
from ..read import harness as harness_mod
from ..read import session as session_mod
from ..store import config as config_mod
from ..store import record as record_mod

# The verdict string a backfilled turn carries. Deliberately not a `Verdict` member: it is
# the *absence* of one, and nothing in the verdict lattice may reduce to it.
UNVERIFIED = "UNVERIFIED"


def available(repo: Path) -> int:
    """How many turns could be backfilled here, without writing anything — what `init` says
    out loud before it does it."""
    return sum(len(_turns_of(t, h, repo)) for t, h in _transcripts(repo))


def run(repo: Path, limit: int | None = None) -> dict:
    """Replay this repo's transcripts into the record. Returns
    ``{"turns": int, "sessions": int, "skipped": int}``.

    Idempotent: a turn already in the record — backfilled before, or verified live while it
    happened — is skipped, matched on (session, start). Re-running after a week of work adds
    only the sessions Tycho didn't see.
    """
    seen = _recorded(repo)
    pending = []
    sessions = 0
    skipped = 0
    for transcript, harness in _transcripts(repo):
        turns = _turns_of(transcript, harness, repo)
        if turns:
            sessions += 1
        for rec in turns:
            if (rec.get("session"), rec.get("started_at")) in seen:
                skipped += 1
                continue
            pending.append(rec)
    # Oldest first, and never more than the file will retain — writing 40,000 rows so the ring
    # can drop 35,000 of them is work nobody sees the result of.
    pending.sort(key=lambda r: r.get("started_at") or 0.0)
    cap = record_mod.max_records() if limit is None else max(1, limit)
    if len(pending) > cap:
        skipped += len(pending) - cap
        pending = pending[-cap:]
    written = sum(record_mod.append(repo, rec) for rec in pending)
    return {"turns": written, "sessions": sessions, "skipped": skipped}


def summary(result: dict) -> list[str]:
    """What a `run` did, for both `tycho backfill` and `tycho init`. Always says what a
    backfilled turn is *not*: a row that reads as a verdict is the one way this can mislead."""
    turns, sessions, skipped = result["turns"], result["sessions"], result["skipped"]
    if not turns:
        if skipped:
            return [f"tycho: nothing new to backfill ({skipped} turn(s) already recorded)."]
        return ["tycho: no past transcripts found for this repo — nothing to backfill."]
    return [
        f"tycho: backfilled {turns} turn{'' if turns == 1 else 's'} from "
        f"{sessions} past session{'' if sessions == 1 else 's'}"
        + (f" ({skipped} already recorded)" if skipped else "") + ".",
        f"  Recorded as {UNVERIFIED} — Tycho wasn't running, so nothing checked them.",
        "  Try: `tycho log` for what your agents did here, `tycho blame <file>` for one file.",
    ]


def _recorded(repo: Path) -> set[tuple]:
    """(session, started_at) for every turn already on the record — the dedup key.

    Not the turn id: the live path stamps `ended_at` from the Stop hook's clock and this one
    from the last event in the turn, so the same turn hashes to two different ids. The start
    boundary is the thing both paths derive identically.
    """
    return {
        (row.get("session"), row.get("started_at"))
        for row in record_mod.iter_records(repo)
    }


def _transcripts(repo: Path) -> list[tuple[Path, harness_mod.Harness]]:
    out = []
    for harness in harness_mod.ENABLED:
        out += [(path, harness) for path in harness.history(repo)]
    return out


def _turns_of(transcript: Path, harness: harness_mod.Harness, repo: Path) -> list[dict]:
    """One transcript → a record per turn it holds. Never raises: a transcript we can't read
    contributes nothing, exactly as it does on the live path."""
    try:
        events = harness.parse(transcript)
        messages = harness.messages(transcript)
        attribution = harness.attribution(transcript)
        starts = events_mod.turn_starts(transcript)
    except (OSError, ValueError):
        return []
    if not events and not messages:
        return []
    # A transcript whose boundaries we can't see is one turn: better a coarse row than none,
    # and `turn_start` already means "the whole transcript" when it returns 0.0.
    bounds = list(starts) or [0.0]
    out = []
    for i, start in enumerate(bounds):
        end = bounds[i + 1] if i + 1 < len(bounds) else float("inf")
        rec = _turn(events, messages, attribution, start, end, harness.name, repo)
        if rec is not None:
            out.append(rec)
    return out


def _turn(events, messages, attribution: Attribution, start: float, end: float,
          harness_name: str, repo: Path) -> dict | None:
    """One turn's window → its record, or None when nothing happened in it."""
    window = tuple(e for e in events if start <= e.ts < end)
    prose = tuple(m for m in messages if start <= m.ts < end)
    if not window and not prose:
        return None
    edits = tuple(
        _relative(fe, repo) for fe in events_mod.file_edits(window)
    )
    ended_at = max(
        [e.ts for e in window if e.ts] + [m.ts for m in prose if m.ts], default=start
    )
    session = Session(
        events=window,
        edits=edits,
        repo=repo,
        config=config_mod.load(repo),
        # Empty on purpose: there is no "now" that was true then. `stage_of` reads `files` to
        # decide `artifact_changed`, and with none it can only report what the transcript
        # proves — a runner ran (`executed`) or it didn't (`attempted`).
        files={},
        git=GitSnapshot(is_repo=False, head_sha=None, changed_paths=()),
        messages=prose,
        attribution=attribution,
        turn_start=start,
    )
    record = record_mod.build(session, [], UNVERIFIED, harness_name, ended_at)
    record["backfilled"] = True
    return record


def _relative(fe, repo: Path):
    """Repo-relative POSIX, the same spelling `gather` stores — `blame` matches on it.

    No git baseline is recovered (`gather._with_baseline`): the blob at today's HEAD is not
    what this edit started from, and nothing here diffs it anyway.
    """
    from dataclasses import replace

    return replace(fe, path=session_mod._relpath(fe.path, repo))

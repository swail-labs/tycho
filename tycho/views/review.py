"""`tycho review` — risk-focus the diff: which part of this do I actually need to read?

**What the signal proves, exactly.** Tycho has no per-line coverage (that would cost the
zero-dependency invariant), so the honest claim is about *the record*, not execution:

    "no command Tycho recorded ran after this hunk was written"

What this module refuses to say is "this line never executed" — a command it never saw (a
manual `pytest`, CI, a debugger) proves nothing to it either way. A review tool that overclaims
coverage is worse than none, so every finding is worded as what was *recorded*.

**Granularity.** Hunks, not files, because `tycho/state.py:88-114` is somewhere to put your
eyes. The evidence itself is per-file (the record stores files, never lines), so hunks in one
file share a verdict — stated in the output rather than blurred.

**Timing is the whole game.** A passing run *before* an edit landed proves nothing about that
edit. Rather than restate that rule, this reads the turn's own recorded `test_freshness` result;
two definitions of "covered" would eventually disagree with the verdict.

**Advisory, always.** `review` ranks and prints; the default exit is OK regardless of what it
found. `unexercised()` backs the opt-in `--exit-code` gate in `cli.py`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..engine import checks as checks_mod
from ..store import config as config_mod
from ..read import gitstate
from ..store import record as record_mod
from ..store import state

# Risk levels, worst first — the list order *is* the ranking.
UNEXERCISED = "UNEXERCISED"    # nothing Tycho recorded ran after this was written
UNTESTED = "NO TEST RUN"       # a command ran after it, but no test runner did
UNRECORDED = "UNRECORDED"      # no recorded turn touched this file at all
TEST = "TEST CHANGED"          # the change *is* a test — a different kind of risk
EXERCISED = "EXERCISED"        # a passing test run was recorded after the last edit
PROSE = "PROSE"                # docs and assets: no test run covers them, by construction

_LEVELS = (UNEXERCISED, UNTESTED, UNRECORDED, TEST, EXERCISED, PROSE)
_ORDER = {level: i for i, level in enumerate(_LEVELS)}
# Levels that get one line each; everything below is one summary line.
_DETAILED = (UNEXERCISED, UNTESTED, UNRECORDED, TEST)
_MARK = {UNEXERCISED: "✗", UNTESTED: "⚠", UNRECORDED: "?", TEST: "•", EXERCISED: "✓", PROSE: "•"}
_HEADLINE = {
    UNEXERCISED: "no recorded command ran after these were written",
    UNTESTED: "a command ran after these, but no test runner did",
    UNRECORDED: "no recorded turn touched these files",
    TEST: "the change is a test — it can't vouch for itself",
}

# How many hunks get their own line before the rest are counted. Past this it's a diff, not a
# review aid.
_MAX_DETAIL = 20
# Bytes of an untracked file we'll read to size it. Beyond this the line range is a formality —
# the whole file is new.
_UNTRACKED_MAX_BYTES = 1 << 20
# The record is written after the write lands, so a small gap is bookkeeping; a larger one
# is somebody else's edit.
_MTIME_SLACK = 2.0


@dataclass(frozen=True)
class Finding:
    """One hunk plus what the record does and doesn't say about it."""

    hunk: gitstate.Hunk
    level: str
    reason: str

    @property
    def rank(self) -> tuple:
        """Worst first; within a level, biggest change first, then a stable path order."""
        return (_ORDER.get(self.level, len(_LEVELS)), -self.hunk.size, self.hunk.path,
                self.hunk.start)


class Findings(list):
    """The findings, plus whether the diff they came from was cut short.

    `--exit-code` reads this: a gate that passes because the diff stopped at `MAX_HUNKS` is
    a gate that passed by not looking.
    """

    truncated = False


def review(repo: Path, since: str = "HEAD") -> list[str]:
    """The rendered review: worst hunks first, everything else counted. Never raises."""
    return inspect(repo, since)[0]


def inspect(repo: Path, since: str = "HEAD") -> tuple[list[str], list[Finding]]:
    """The rendered review *and* the findings behind it, in one pass.

    Re-deriving them for the `--exit-code` gate would mean a second `git diff` and a second pass
    over the record. Returns `[]` findings on every can't-say path, so "no findings" is never
    mistaken for "nothing was wrong".
    """
    if not gitstate.is_repo(repo):
        return ["tycho: not a git repository — nothing to review."], []
    hunks = gitstate.diff_hunks(repo, since)
    if hunks is None and since != "HEAD" and not gitstate.commit_exists(repo, since):
        return [f"tycho: can't diff against {since} — no such commit."], []
    untracked = _untracked_hunks(repo)
    blind = hunks is None
    if blind:
        # The ref doesn't resolve (usually a fresh repo with no commits). The untracked files are
        # still real, so review those and say what's missing.
        if not untracked:
            return [f"tycho: can't diff against {since} — nothing to compare."], []
        hunks = ()
    seen = (*hunks, *untracked)
    all_hunks = tuple(h for h in seen if not _is_own_state(repo, h.path))
    skipped = len(seen) - len(all_hunks)
    if not all_hunks:
        if skipped:
            # Never "no changes" when there were changes: the filter exists to hide our own
            # install noise, and silence here would also hide an agent editing that install.
            return [f"tycho: no changes against {since} outside Tycho's own files "
                    f"— {skipped} hunk(s) skipped."], Findings()
        return [f"tycho: no changes against {since}."], Findings()
    findings = classify(repo, all_hunks)
    findings.truncated = len(hunks) >= gitstate.MAX_HUNKS
    lines = render(findings, since, truncated=findings.truncated, skipped=skipped)
    if blind:
        lines.insert(0, f"tycho: can't diff against {since} — untracked files only.")
    return lines, findings


def classify(repo: Path, hunks, now: float | None = None) -> Findings:
    """Rank `hunks` by what the record can say about them, in one pass. `now` is injectable so
    the ages in the reasons are reproducible in a test."""
    now = time.time() if now is None else now
    paths = {h.path for h in hunks}
    facts = _facts(repo, paths)
    mtimes = {p: _mtime(repo, p) for p in paths}
    changed_tests = [p for p in paths if checks_mod._is_test_path(p)]
    findings = [Finding(h, *_judge(h, facts, changed_tests, now, mtimes.get(h.path)))
                for h in hunks]
    return Findings(sorted(findings, key=lambda f: f.rank))


def unexercised(findings) -> int:
    """How many findings `--exit-code` should exit non-zero for.

    A truncated diff counts as one: the hunks past the cap were never judged, so "nothing
    found" is "nothing looked at" and the gate must not read it as clean.
    """
    found = sum(1 for f in findings if f.level in (UNEXERCISED, UNTESTED))
    return found or (1 if getattr(findings, "truncated", False) else 0)


# --- the record side ---------------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    """What one streaming pass over `turns.jsonl` learned. Timestamps, not judgements."""

    edited_at: dict[str, float]   # path → newest ts a record says it was edited
    last_test: float | None       # newest recorded *passing test runner*
    last_command: float | None    # newest recorded passing command of any kind


def _facts(repo: Path, paths: set[str]) -> _Facts:
    """One pass, oldest→newest, keeping only aggregates for `paths`.

    Memory is O(len(paths)) whatever the record's size. `iter_records` skips corrupt lines; the
    isinstance guards cover the other half — a well-formed line carrying a junk type, which
    would blow up on a comparison.
    """
    edited: dict[str, float] = {}
    last_test = last_command = None
    for row in record_mod.iter_records(repo):
        ran = _run_ts(row)
        if ran is not None:
            passed_test, passed_any, ts = ran
            if passed_test:
                last_test = ts if last_test is None else max(last_test, ts)
            if passed_any:
                last_command = ts if last_command is None else max(last_command, ts)
        for entry in record_mod._rows(row, "files"):
            path = entry.get("path")
            ts = _num(entry.get("ts")) or _num(row.get("ended_at"))
            if isinstance(path, str) and path in paths and ts is not None:
                edited[path] = max(edited.get(path, ts), ts)
    return _Facts(edited, last_test, last_command)


def _run_ts(row: dict) -> tuple[bool, bool, float] | None:
    """(a test runner passed, some command passed, when) for one record — or None.

    **When** is the subtle part. The record stores per-turn bounds, not per-command timestamps,
    so `ended_at` is the latest a command could have run — and using it would claim a run covered
    an edit made later in the same turn. So when the turn itself recorded `test_freshness` STALE,
    the run is dated to `started_at`: the earliest it could have been, the side that under-claims
    coverage. Reading that check's own result rather than re-deriving it keeps the two agreeing.
    """
    passed_test = passed_any = False
    for c in record_mod._rows(row, "commands"):
        if c.get("outcome") != "passed":
            continue
        passed_any = True
        passed_test = passed_test or bool(c.get("runner"))
    if not passed_any:
        return None
    ended = _num(row.get("ended_at"))
    started = _num(row.get("started_at"))
    ts = started if (_stale(row) and started is not None) else ended
    return (passed_test, passed_any, ts) if ts is not None else None


def _stale(row: dict) -> bool:
    """Did this turn's own `test_freshness` say a source outran the passing run?"""
    return any(
        c.get("name") == "test_freshness" and c.get("status") == "STALE"
        for c in record_mod._rows(row, "checks")
    )


def _num(value) -> float | None:
    """A timestamp, or None. Records are durable and old ones may predate a field."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --- the judgement -----------------------------------------------------------


def _judge(hunk, facts: _Facts, changed_tests, now: float,
           mtime: float | None = None) -> tuple[str, str]:
    """One hunk → (level, reason). The reasons state what was recorded, never what ran.

    The record only sees an agent's Edit/Write calls. A human editor, a shell redirect or a
    `sed -i` leaves nothing in it, so the file's own mtime is the other half of "when was this
    last written" — and the later of the two is the only honest one to judge a run against.
    """
    path = hunk.path
    note = _status_note(hunk)
    edited = facts.edited_at.get(path)
    if checks_mod._is_prose_path(path):
        return PROSE, note + "prose or asset — no test run covers it"
    if edited is None:
        return UNRECORDED, note + "no recorded turn touched this file"
    verb = "edited"
    if mtime is not None and mtime > edited + _MTIME_SLACK:
        edited, verb = mtime, "changed on disk (not by a recorded edit)"
    age = _elapsed(now - edited)
    if checks_mod._is_test_path(path):
        if facts.last_test is not None and edited > facts.last_test:
            return TEST, note + f"{verb} {age}, after the last recorded passing run"
        return TEST, note + f"{verb} {age} — a changed test is evidence, not proof"
    if facts.last_test is not None and facts.last_test >= edited:
        return EXERCISED, note + f"a passing test run was recorded after the edit ({age})"
    if facts.last_command is not None and facts.last_command >= edited:
        return UNTESTED, note + f"{verb} {age}; a command ran after it, but no test runner"
    tail = "" if _has_sibling_test(path, changed_tests) else "; no test file changed alongside"
    if facts.last_test is None and facts.last_command is None:
        return UNEXERCISED, note + f"{verb} {age}; no passing command in any recorded turn{tail}"
    return UNEXERCISED, note + f"{verb} {age}; no recorded command ran after it{tail}"


def _mtime(repo: Path, path: str) -> float | None:
    """The file's last-modified time, or None when it isn't there to ask (deleted, unreadable)."""
    try:
        return (repo / path).stat().st_mtime
    except (OSError, ValueError):
        return None


def _status_note(hunk) -> str:
    """The one-word prefix for a hunk that isn't a plain edit, or "" for one that is."""
    return {
        "added": "new file — ",
        "untracked": "new, untracked — ",
        "deleted": "deleted — ",
        "renamed": "renamed — ",
        "binary": "binary — ",
        "mode": "mode change only — ",
        "unparsed": "diff not parseable, read the whole file — ",
    }.get(hunk.status, "")


def _has_sibling_test(path: str, changed_tests) -> bool:
    """Did a test file that looks like it belongs to `path` change in this same diff?

    ponytail: stem-in-basename, catching `test_foo.py`, `foo_test.go`, `foo.test.ts`,
    `FooTest.java`. It over-matches on the quiet side — it only ever suppresses a trailing note,
    never creates a finding.
    """
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0].lower()
    return bool(stem) and any(stem in t.rsplit("/", 1)[-1].lower() for t in changed_tests)


def _elapsed(seconds: float) -> str:
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now" if seconds >= 0 else "in the future"


# --- untracked files ---------------------------------------------------------


def _untracked_hunks(repo: Path) -> tuple[gitstate.Hunk, ...]:
    """New files git can't diff, as one whole-file hunk each. Never raises on unreadable."""
    out = []
    for rel in gitstate.untracked(repo):
        lines, binary = _size_of(repo / rel)
        if binary:
            out.append(gitstate.Hunk(rel, 0, 0, 0, 0, "binary"))
        elif lines:
            out.append(gitstate.Hunk(rel, 1, lines, lines, 0, "untracked"))
    return tuple(out)


def _is_own_state(repo: Path, path: str) -> bool:
    """Exactly what `tycho init` writes — ours to install, never the user's code to review.

    Measured on a freshly-initialised repo: 21 of 24 hunks were Tycho's own files (the
    settings file, the config, and 19 slash-command docs), so the first review a new user
    ever ran was almost entirely Tycho reporting on itself. `.tycho/` is gitignored by init
    now, but only from the moment it runs — a repo mid-install still shows it.

    Named files only, never the whole of `.claude/`: an agent's own hooks, agents and
    commands live there too, and they are the user's code however much they look like ours.
    """
    p = path.replace("\\", "/")
    return (
        p.startswith(state.dir_for(repo).name + "/")
        or p == ".claude/settings.json"
        or (p.startswith(".claude/commands/tycho-") and p.endswith(".md"))
        or p == config_mod.CONFIG_NAME
    )


def _size_of(path: Path) -> tuple[int, bool]:
    """(line count, looks binary). (0, False) for anything we can't read — say nothing."""
    try:
        blob = path.open("rb").read(_UNTRACKED_MAX_BYTES)
    except OSError:
        return 0, False
    if b"\0" in blob[:8192]:
        return 0, True
    return blob.count(b"\n") + (1 if blob and not blob.endswith(b"\n") else 0), False


# --- rendering ---------------------------------------------------------------


def render(findings, since: str, truncated: bool = False, skipped: int = 0) -> list[str]:
    """Worst first, aligned, and honest about its own limits in the last line.

    No colour, ever — not even on a tty: a review piped into a file or a PR comment is a
    first-class use, and marks plus indentation carry it.
    """
    files = len({f.hunk.path for f in findings})
    head = f"tycho review — {len(findings)} hunk(s) in {files} file(s) changed against {since}"
    notes = ([f"truncated at {gitstate.MAX_HUNKS}"] if truncated else []) + (
        [f"{skipped} hunk(s) skipped, Tycho's own files"] if skipped else [])
    lines = [head + (f" ({', '.join(notes)})" if notes else ""), ""]
    shown = 0
    for level in _DETAILED:
        group = [f for f in findings if f.level == level]
        if not group:
            continue
        lines.append(f"  {_MARK[level]} {level} — {_HEADLINE[level]}")
        budget = max(0, _MAX_DETAIL - shown)
        width = max((len(f.hunk.ref) for f in group[:budget]), default=0)
        for f in group[:budget]:
            lines.append(f"      {f.hunk.ref:<{width}}  {f.reason}")
        if len(group) > budget:
            lines.append(f"      … and {len(group) - budget} more")
        shown += len(group[:budget])
        lines.append("")
    for level in (EXERCISED, PROSE):
        group = [f for f in findings if f.level == level]
        if group:
            lines.append(f"  {_MARK[level]} {len(group)} hunk(s) {_summary(level, group)}")
    if lines[-1]:
        lines.append("")
    lines.append('  "Exercised" means a command Tycho recorded ran after the hunk was written.')
    lines.append("  Tycho has no per-line coverage — it cannot say these lines executed.")
    if truncated:
        lines.append(f"  The diff was cut at {gitstate.MAX_HUNKS} hunks — changes past it were "
                     "not reviewed at all.")
    return lines


def _summary(level: str, group) -> str:
    if level == EXERCISED:
        return f"exercised: a passing test run recorded after the last edit ({_files(group)})"
    return f"prose or assets, which no test run covers ({_files(group)})"


def _files(group) -> str:
    paths = sorted({f.hunk.path for f in group})
    return paths[0] if len(paths) == 1 else f"{len(paths)} files"

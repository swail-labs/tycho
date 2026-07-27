"""`tycho review` — risk-focus the diff (strategy §9.4).

*"These 3 hunks were touched by no test and exercised by no command."* Change-aware
coverage, pitched as a review aid rather than a check. It answers the most expensive
question a developer has — *which part of this do I actually need to read?* — and unlike a
lie detector it doesn't decay, because it makes a coverage claim, not a correctness one.

**What the signal actually proves, exactly.** Tycho has no per-line coverage: no
coverage.py, no instrumentation, no import hook — and adding one would cost the
zero-dependency invariant that is the reason this runs on every repo. So the honest claim
is about *the record*, not about execution:

    "no command Tycho recorded ran after this hunk was written"

That is a fact about the turn record and the diff, and it is checkable. What this module
refuses to say is "this line never executed" — it cannot know that, because a command it
never saw (a manual `pytest` in another terminal, CI, a debugger) proves nothing to it
either way. A review tool that overclaims coverage is worse than no review tool, so every
finding here is worded as what was *recorded*, not what happened (TYCHO-125).

**Granularity.** Hunks, not files: "this file wasn't covered" is unactionable on a
900-line file. The coverage evidence itself is per-file — the record stores which files a
turn touched, never which lines — so hunks in one file share a verdict. What hunks buy is
the address: `tycho/state.py:88-114` is somewhere to put your eyes, `tycho/state.py` is a
chore. That split is stated in the output rather than blurred.

**Timing is the whole game.** A passing test run *before* an edit landed proves nothing
about that edit — it's the same reasoning `checks.test_freshness` encodes as STALE, and it
is mirrored here rather than restated, down to reading the recorded `test_freshness`
result when deciding what a turn's run covered. Two definitions of "covered" would
eventually disagree with the verdict, which is the split-brain `checks._outcome` exists to
prevent.

**Advisory, always.** `review` ranks and prints; it does not gate. The strategy doc
demotes PR-blocking CI as a distraction that drags the roadmap toward "AI code review
vendor" (§6), so the default exit is OK regardless of what it found. `unexercised()` is
here for a caller that wants to opt into a non-zero exit; nothing calls it today.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import checks as checks_mod
from . import gitstate
from . import record as record_mod
from . import state

# Risk levels, worst first — the list order *is* the ranking, so `_ORDER` never needs
# maintaining separately from the constants.
UNEXERCISED = "UNEXERCISED"    # nothing Tycho recorded ran after this was written
UNTESTED = "NO TEST RUN"       # a command ran after it, but no test runner did
UNRECORDED = "UNRECORDED"      # no recorded turn touched this file at all
TEST = "TEST CHANGED"          # the change *is* a test — a different kind of risk
EXERCISED = "EXERCISED"        # a passing test run was recorded after the last edit
PROSE = "PROSE"                # docs and assets: no test run covers them, by construction

_LEVELS = (UNEXERCISED, UNTESTED, UNRECORDED, TEST, EXERCISED, PROSE)
_ORDER = {level: i for i, level in enumerate(_LEVELS)}
# Levels that get one line each, in order. Everything below is one summary line: a wall of
# every hunk is a wall nobody reads (§4 has a direct user warning about exactly that).
_DETAILED = (UNEXERCISED, UNTESTED, UNRECORDED, TEST)
_MARK = {UNEXERCISED: "✗", UNTESTED: "⚠", UNRECORDED: "?", TEST: "•", EXERCISED: "✓", PROSE: "•"}
_HEADLINE = {
    UNEXERCISED: "no recorded command ran after these were written",
    UNTESTED: "a command ran after these, but no test runner did",
    UNRECORDED: "no recorded turn touched these files",
    TEST: "the change is a test — it can't vouch for itself",
}

# How many hunks get their own line before the rest are counted. Past this the output has
# stopped being a review aid and started being a diff.
_MAX_DETAIL = 20
# Bytes of an untracked file we'll read to size it. Beyond this the line range is a
# formality anyway — the whole file is new, so the range is "all of it".
_UNTRACKED_MAX_BYTES = 1 << 20


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


def review(repo: Path, since: str = "HEAD") -> list[str]:
    """The rendered review: worst hunks first, everything else counted. Never raises."""
    return inspect(repo, since)[0]


def inspect(repo: Path, since: str = "HEAD") -> tuple[list[str], list[Finding]]:
    """The rendered review *and* the findings behind it, in one pass.

    `review` is the printing surface; a caller that also wants to act on the result — the
    `--exit-code` gate in cli.py — needs the findings too, and re-deriving them would mean a
    second `git diff` and a second pass over the record. Returns `[]` findings on every
    can't-say path, so "no findings" is never mistaken for "nothing was wrong".
    """
    if not gitstate.is_repo(repo):
        return ["tycho: not a git repository — nothing to review."], []
    hunks = gitstate.diff_hunks(repo, since)
    if hunks is None and since != "HEAD" and not gitstate.commit_exists(repo, since):
        return [f"tycho: can't diff against {since} — no such commit."], []
    untracked = _untracked_hunks(repo)
    blind = hunks is None
    if blind:
        # The ref doesn't resolve (a fresh repo with no commits is the common one). The
        # untracked files are still real, so review those and say what's missing.
        if not untracked:
            return [f"tycho: can't diff against {since} — nothing to compare."], []
        hunks = ()
    all_hunks = tuple(h for h in (*hunks, *untracked) if not _is_own_state(repo, h.path))
    if not all_hunks:
        return [f"tycho: no changes against {since}."], []
    findings = classify(repo, all_hunks)
    lines = render(findings, since, truncated=len(hunks) >= gitstate.MAX_HUNKS)
    if blind:
        lines.insert(0, f"tycho: can't diff against {since} — untracked files only.")
    return lines, findings


def classify(repo: Path, hunks, now: float | None = None) -> list[Finding]:
    """Rank `hunks` by what the record can say about them. One pass over the record.

    `now` is injectable so the ages in the reasons are reproducible in a test — the same
    reason a record's `ended_at` is passed into `record.build` rather than read from a clock.
    """
    now = time.time() if now is None else now
    paths = {h.path for h in hunks}
    facts = _facts(repo, paths)
    changed_tests = [p for p in paths if checks_mod._is_test_path(p)]
    findings = [Finding(h, *_judge(h, facts, changed_tests, now)) for h in hunks]
    return sorted(findings, key=lambda f: f.rank)


def unexercised(findings) -> int:
    """How many findings are the ones a caller might want a non-zero exit for.

    Not used by `tycho review` itself, which is advisory by design (see the module
    docstring). Here so opting in is a flag in `cli.py`, not a rewrite of this module.
    """
    return sum(1 for f in findings if f.level in (UNEXERCISED, UNTESTED))


# --- the record side ---------------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    """What one streaming pass over `turns.jsonl` learned. Timestamps, not judgements."""

    edited_at: dict[str, float]   # path → newest ts a record says it was edited
    last_test: float | None       # newest recorded *passing test runner*
    last_command: float | None    # newest recorded passing command of any kind


def _facts(repo: Path, paths: set[str]) -> _Facts:
    """One pass, oldest→newest, keeping only aggregates for `paths`.

    Bounded by construction: memory is O(len(paths)) whatever the record's size, so a repo
    with 5000 turns costs the same as one with five. `iter_records` already skips corrupt
    lines; the isinstance guards here cover the other half of that — a *well-formed* line
    carrying a junk type, which would otherwise blow up on a comparison.
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
        files = row.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            ts = _num(entry.get("ts")) or _num(row.get("ended_at"))
            if isinstance(path, str) and path in paths and ts is not None:
                edited[path] = max(edited.get(path, ts), ts)
    return _Facts(edited, last_test, last_command)


def _run_ts(row: dict) -> tuple[bool, bool, float] | None:
    """(a test runner passed, some command passed, when) for one record — or None.

    **When** is the subtle part. The record stores per-turn bounds, not per-command
    timestamps, so the exact instant a command ran is not recoverable; the turn's
    `ended_at` is the latest it could have been. Using that would quietly claim a run
    covered an edit made later in the same turn — which is precisely what
    `checks.test_freshness` reports as STALE. So when the turn itself recorded STALE, the
    run is dated to `started_at` instead: the earliest it could have been, which is the
    side of the trade that under-claims coverage. The two can't disagree, because this
    reads that check's own result rather than re-deriving it.
    """
    commands = row.get("commands")
    if not isinstance(commands, list):
        return None
    passed_test = passed_any = False
    for c in commands:
        if not isinstance(c, dict) or c.get("outcome") != "passed":
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
    checks = row.get("checks")
    if not isinstance(checks, list):
        return False
    return any(
        isinstance(c, dict) and c.get("name") == "test_freshness" and c.get("status") == "STALE"
        for c in checks
    )


def _num(value) -> float | None:
    """A timestamp, or None. Records are durable and old ones may predate a field."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --- the judgement -----------------------------------------------------------


def _judge(hunk, facts: _Facts, changed_tests, now: float) -> tuple[str, str]:
    """One hunk → (level, reason). The reasons state what was recorded, never what ran."""
    path = hunk.path
    note = _status_note(hunk)
    edited = facts.edited_at.get(path)
    if checks_mod._is_prose_path(path):
        return PROSE, note + "prose or asset — no test run covers it"
    if edited is None:
        return UNRECORDED, note + "no recorded turn touched this file"
    age = _ago(now - edited)
    if checks_mod._is_test_path(path):
        if facts.last_test is not None and edited > facts.last_test:
            return TEST, note + f"edited {age}, after the last recorded passing run"
        return TEST, note + f"edited {age} — a changed test is evidence, not proof"
    if facts.last_test is not None and facts.last_test >= edited:
        return EXERCISED, note + f"a passing test run was recorded after the edit ({age})"
    if facts.last_command is not None and facts.last_command >= edited:
        return UNTESTED, note + f"edited {age}; a command ran after it, but no test runner"
    tail = "" if _has_sibling_test(path, changed_tests) else "; no test file changed alongside"
    if facts.last_test is None and facts.last_command is None:
        return UNEXERCISED, note + f"edited {age}; no passing command in any recorded turn{tail}"
    return UNEXERCISED, note + f"edited {age}; no recorded command ran after it{tail}"


def _status_note(hunk) -> str:
    """The one-word prefix for a hunk that isn't a plain edit, or "" for one that is."""
    return {
        "added": "new file — ",
        "untracked": "new, untracked — ",
        "deleted": "deleted — ",
        "renamed": "renamed — ",
        "binary": "binary — ",
        "unparsed": "diff not parseable, read the whole file — ",
    }.get(hunk.status, "")


def _has_sibling_test(path: str, changed_tests) -> bool:
    """Did a test file that looks like it belongs to `path` change in this same diff?

    ponytail: stem-in-basename, which catches `test_foo.py`, `foo_test.go`, `foo.test.ts`
    and `FooTest.java`. It over-matches (`test_foobar.py` claims `foo.py`) on the side that
    stays quiet rather than the side that cries wolf — this only ever *suppresses* a
    trailing note, never creates a finding.
    """
    stem = path.replace("\\", "/").rsplit("/", 1)[-1].split(".", 1)[0].lower()
    return bool(stem) and any(stem in t.rsplit("/", 1)[-1].lower() for t in changed_tests)


def _ago(seconds: float) -> str:
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
    """Tycho's own `.tycho/` directory. Nobody reviews the verifier's state file, and it is
    not gitignored, so an un-init'd repo would otherwise open every review with our noise."""
    return path.replace("\\", "/").startswith(state.dir_for(repo).name + "/")


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


def render(findings, since: str, truncated: bool = False) -> list[str]:
    """Worst first, aligned, and honest about its own limits in the last line.

    No colour, ever — not conditionally on a tty, just none. `doctor` and `report` carry
    this whole family on marks and indentation alone, and a review that is piped into a
    file or a PR comment is a first-class use, not a degraded one.
    """
    files = len({f.hunk.path for f in findings})
    head = f"tycho review — {len(findings)} hunk(s) in {files} file(s) changed against {since}"
    lines = [head + (" (truncated)" if truncated else ""), ""]
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
    return lines


def _summary(level: str, group) -> str:
    if level == EXERCISED:
        return f"exercised: a passing test run recorded after the last edit ({_files(group)})"
    return f"prose or assets, which no test run covers ({_files(group)})"


def _files(group) -> str:
    paths = sorted({f.hunk.path for f in group})
    return paths[0] if len(paths) == 1 else f"{len(paths)} files"

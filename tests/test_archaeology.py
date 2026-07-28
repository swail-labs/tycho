"""§9.3 — `tycho blame` / `tycho log`: the archaeology surface.

Three things are worth guarding here, and none of them is that the output is pretty.

The first is **honesty about what the record knows**. `blame src/app.py:42` is in the
strategy doc's own example, but the record stores files, not lines. A line that quietly
reads as line-42 attribution would be a confident wrong answer — the one failure mode this
codebase never accepts (every check returns UNSUPPORTED with a reason instead). So the
`:LINE` note is asserted, and asserted *before* the results.

The second is the **evidence clause**. "no test ran" / "pytest passed" / "never verified"
is the whole reason this isn't a worse `git log`, and each state has to come out of the
record rather than out of a guess.

The third is that it **never raises**. A record missing every optional field, a null `model`
or `session` (both nullable by design — `record.py` never guesses attribution) must each cost
one row at most, never the command. (Corrupt JSONL lines are `record.iter_records`' job and
are guarded in `test_record.py`.)
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import turn_record

from tycho.views import archaeology
from tycho.store import record

NOW = 1_800_000_000.0  # a fixed clock: relative times are asserted, so they can't float


@pytest.fixture(autouse=True)
def _no_colour(monkeypatch):
    """Assert on text, not escape codes. Colour has its own tests."""
    monkeypatch.setenv("NO_COLOR", "1")


def turn(
    path: str | list[str] = "src/app.py",
    claim: str = "fixed the retry logic",
    verdict: str = "VERIFIED",
    stage: str = "claim_supported",
    checks: list[dict] | None = None,
    commands: list[dict] | None = None,
    ended_at: float = NOW - 3600,
    id: str = "a1b2c3d4e5f60718",
    model: str | None = "claude-opus-5",
    session: str | None = "sess-1",
) -> dict:
    """One record in the shape `record.build` writes. Defaults to the happy turn."""
    paths = [path] if isinstance(path, str) else path
    return turn_record(
        id=id, session=session, model=model,
        started_at=ended_at - 60, ended_at=ended_at, verdict=verdict, stage=stage,
        checks=[{"name": "command_execution", "status": "PASS", "evidence": "ran"}]
        if checks is None else checks,
        files=[{"path": p, "kind": "edit", "ts": ended_at - 30} for p in paths],
        commands=[{"cmd": "pytest -q", "runner": True, "outcome": "passed"}]
        if commands is None else commands,
        claims=[claim] if claim else [],
    )


def write(repo: Path, *records: dict) -> Path:
    """Write records oldest-first, exactly as `record.append` would have."""
    path = record.path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def blame(repo: Path, target: str, **kw) -> list[str]:
    return archaeology.blame(repo, target, now=NOW, cwd=kw.pop("cwd", repo), **kw)


def log(repo: Path, **kw) -> list[str]:
    return archaeology.log(repo, now=NOW, **kw)


# --- target parsing ----------------------------------------------------------


def test_trailing_line_number_is_split_off():
    assert archaeology.parse_target("src/app.py:42") == archaeology.Target("src/app.py", 42)


def test_a_bare_path_has_no_line():
    assert archaeology.parse_target("src/app.py") == archaeology.Target("src/app.py", None)


@pytest.mark.parametrize("target", [
    r"C:\src\app.py",          # a Windows drive letter is not a line number
    "src/app.py:42:",          # trailing junk — not the PATH:LINE shape
    "src/weird:name.py",       # a colon inside the filename
])
def test_only_a_trailing_integer_counts_as_a_line(target: str):
    assert archaeology.parse_target(target) == archaeology.Target(target)


# --- path resolution ---------------------------------------------------------


def test_repo_relative_path_passes_through(tmp_path: Path):
    assert archaeology.resolve(tmp_path, "src/app.py", cwd=tmp_path) == "src/app.py"


def test_absolute_path_becomes_repo_relative(tmp_path: Path):
    absolute = str(tmp_path / "src" / "app.py")
    assert archaeology.resolve(tmp_path, absolute, cwd=tmp_path) == "src/app.py"


def test_path_relative_to_a_subdirectory_resolves_against_the_repo_root(tmp_path: Path):
    """`state.root_for` keys state to the repo root; the developer stands in `src/`."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    assert archaeology.resolve(tmp_path, "app.py", cwd=tmp_path / "src") == "src/app.py"
    assert archaeology.resolve(tmp_path, "../src/app.py", cwd=tmp_path / "src") == "src/app.py"


def test_bare_basename_that_is_not_here_stays_a_basename(tmp_path: Path):
    """`record.touching` matches a basename in any directory — someone typing `app.py`
    from the wrong directory means "find it", not "look only here"."""
    (tmp_path / "src").mkdir()
    assert archaeology.resolve(tmp_path, "app.py", cwd=tmp_path / "src") == "app.py"


def test_a_path_outside_the_repo_survives_as_itself(tmp_path: Path):
    outside = tmp_path.parent / "elsewhere" / "app.py"
    # POSIX-separated even on Windows: `resolve` normalizes before it decides, and the record
    # it feeds stores POSIX. See test_windows_separators_are_normalized.
    assert archaeology.resolve(tmp_path / "repo", str(outside)) == outside.as_posix()


def test_windows_separators_are_normalized(tmp_path: Path):
    assert archaeology.resolve(tmp_path, r"src\app.py", cwd=tmp_path) == "src/app.py"


def test_blame_finds_the_turn_from_a_subdirectory(tmp_path: Path):
    write(tmp_path, turn(path="src/app.py"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    lines = blame(tmp_path, "app.py", cwd=tmp_path / "src")
    assert lines[0].startswith("src/app.py — 1 turn")


def test_blame_finds_the_turn_from_an_absolute_path(tmp_path: Path):
    write(tmp_path, turn(path="src/app.py"))
    assert blame(tmp_path, str(tmp_path / "src/app.py"))[0].startswith("src/app.py — 1 turn")


# --- :LINE honesty -----------------------------------------------------------


def test_line_number_is_acknowledged_and_declared_file_level(tmp_path: Path):
    write(tmp_path, turn())
    lines = blame(tmp_path, "src/app.py:42")
    assert "asked for :42" in lines[1]
    assert "file-level" in lines[1]
    assert "not which lines" in lines[1]


def test_the_line_caveat_comes_before_the_results(tmp_path: Path):
    """A reader who takes row one as line-42 attribution has already been misled by the
    time a footnote arrives."""
    write(tmp_path, turn())
    lines = blame(tmp_path, "src/app.py:42")
    assert "file-level" in lines[1]
    assert not any("file-level" in ln for ln in lines[2:])


def test_a_line_number_never_narrows_the_result_set(tmp_path: Path):
    """Honest degradation: `:42` shows the same turns as the bare path, and says so."""
    write(tmp_path, turn(id="a" * 16), turn(id="b" * 16))
    with_line = [ln for ln in blame(tmp_path, "src/app.py:42") if "file-level" not in ln]
    assert with_line == blame(tmp_path, "src/app.py")


def test_no_line_number_means_no_caveat(tmp_path: Path):
    write(tmp_path, turn())
    assert not any("file-level" in ln for ln in blame(tmp_path, "src/app.py"))


# --- the evidence clause -----------------------------------------------------


def evidence_of(repo: Path, record_: dict) -> str:
    write(repo, record_)
    return blame(repo, "src/app.py")[2]


def test_a_passing_runner_names_the_command(tmp_path: Path):
    assert "pytest -q passed" in evidence_of(tmp_path, turn())


def test_a_failing_runner_says_so(tmp_path: Path):
    line = evidence_of(tmp_path, turn(
        verdict="FAILED",
        checks=[{"name": "command_execution", "status": "PASS", "evidence": "ran"}],
        commands=[{"cmd": "pytest -q", "runner": True, "outcome": "failed"}],
    ))
    assert "pytest -q failed" in line


def test_a_masked_exit_status_is_reported_as_unknown(tmp_path: Path):
    line = evidence_of(tmp_path, turn(
        commands=[{"cmd": "pytest -q | tee out", "runner": True, "outcome": "unknown"}]))
    assert "exit status unknown" in line


def test_no_runner_but_something_checked_is_no_test_ran(tmp_path: Path):
    line = evidence_of(tmp_path, turn(
        checks=[{"name": "scope_drift", "status": "PASS", "evidence": "all edits in scope"}],
        commands=[{"cmd": "ls", "runner": False, "outcome": "passed"}],
    ))
    assert line.endswith("— no test ran")


def test_nothing_ran_and_nothing_concluded_is_never_verified(tmp_path: Path):
    """Distinct from "no test ran": nobody tested it and nobody could even look."""
    line = evidence_of(tmp_path, turn(
        checks=[{"name": "command_execution", "status": "UNSUPPORTED", "evidence": "none ran"}],
        commands=[],
    ))
    assert "never verified" in line


def test_no_checks_at_all_is_never_verified(tmp_path: Path):
    assert "never verified" in evidence_of(tmp_path, turn(checks=[], commands=[]))


def test_an_adverse_check_outranks_a_passing_runner(tmp_path: Path):
    """Tycho already wrote the truest sentence about the turn — don't paraphrase past it."""
    line = evidence_of(tmp_path, turn(
        verdict="STALE",
        checks=[{"name": "test_freshness", "status": "STALE",
                 "evidence": "src/app.py edited after the last passing run"},
                {"name": "command_execution", "status": "PASS", "evidence": "ran"}],
    ))
    assert "test_freshness: src/app.py edited after the last passing run" in line
    assert "passed" not in line


def test_a_failure_outranks_a_pass_among_several_runners(tmp_path: Path):
    line = evidence_of(tmp_path, turn(commands=[
        {"cmd": "pytest tests/a.py", "runner": True, "outcome": "passed"},
        {"cmd": "pytest tests/b.py", "runner": True, "outcome": "failed"},
    ]))
    assert "pytest tests/b.py failed" in line


# --- claims ------------------------------------------------------------------


def test_the_claim_is_the_closing_message_not_the_first(tmp_path: Path):
    """The last message is the summary; the earlier ones are narration mid-work."""
    rec = turn()
    rec["claims"] = ["Looking at the retry path now.", "Fixed the retry logic."]
    write(tmp_path, rec)
    assert '"Fixed the retry logic."' in blame(tmp_path, "src/app.py")[2]


def test_a_multi_line_claim_becomes_one_line(tmp_path: Path):
    write(tmp_path, turn(claim="Two files added:\n\n- **docs/x.md**\n- **.github/y.yml**"))
    line = blame(tmp_path, "src/app.py")[2]
    assert line.startswith('    "Two files added:" — ')


def test_a_turn_with_no_prose_says_so(tmp_path: Path):
    write(tmp_path, turn(claim=""))
    assert '"(no claim recorded)"' in blame(tmp_path, "src/app.py")[2]


def test_a_long_claim_is_truncated_to_one_readable_line(tmp_path: Path):
    write(tmp_path, turn(claim="word " * 200))
    lines = blame(tmp_path, "src/app.py")
    assert all(len(ln) <= 100 for ln in lines), lines
    assert "…" in lines[2]


def test_the_evidence_survives_a_long_claim(tmp_path: Path):
    """The claim absorbs the truncation — the clause is what makes this Tycho."""
    write(tmp_path, turn(claim="word " * 200, commands=[]))
    assert blame(tmp_path, "src/app.py")[2].endswith("— no test ran")


# --- empty states ------------------------------------------------------------


def test_a_file_no_turn_touched_says_so_and_says_why(tmp_path: Path):
    write(tmp_path, turn(path="src/other.py"))
    lines = blame(tmp_path, "src/app.py")
    assert lines[0] == "tycho: no recorded turn touched src/app.py."
    assert "Stop hook" in lines[1]
    assert "`tycho log`" in lines[2]


def test_a_repo_with_no_records_at_all_reads_differently(tmp_path: Path):
    """"Tycho has nothing" and "Tycho has history, just not for this file" are different news."""
    lines = blame(tmp_path, "src/app.py")
    assert "no turns recorded in this repo yet" in lines[0]
    assert "Stop hook" in lines[1]


def test_log_with_no_records_says_why(tmp_path: Path):
    assert log(tmp_path) == ["tycho: no turns recorded yet.",
                             f"       {archaeology._WHY_EMPTY}"]


def test_blame_with_an_empty_target_asks_for_a_path(tmp_path: Path):
    assert blame(tmp_path, "")[0].startswith("tycho blame: give it a path")


def test_log_with_a_filter_that_matches_nothing(tmp_path: Path):
    write(tmp_path, turn())
    assert log(tmp_path, verdict="FAILED") == ["tycho: no recorded turn matches that filter."]


# --- ordering, limits, boundedness -------------------------------------------


def test_blame_is_newest_first(tmp_path: Path):
    write(tmp_path,
          turn(claim="older", ended_at=NOW - 7200, id="a" * 16),
          turn(claim="newer", ended_at=NOW - 60, id="b" * 16))
    lines = blame(tmp_path, "src/app.py")
    assert '"newer"' in lines[2] and '"older"' in lines[4]


def test_log_is_newest_first(tmp_path: Path):
    write(tmp_path,
          turn(claim="older", ended_at=NOW - 7200, id="a" * 16),
          turn(claim="newer", ended_at=NOW - 60, id="b" * 16))
    assert '"newer"' in log(tmp_path)[0] and '"older"' in log(tmp_path)[1]


def test_log_honours_its_limit(tmp_path: Path):
    write(tmp_path, *(turn(id=f"{i:016x}") for i in range(30)))
    assert len(log(tmp_path, limit=5)) == 5


@pytest.mark.parametrize("limit", [0, -5])
def test_a_non_positive_limit_is_refused_rather_than_answered_wrongly(tmp_path: Path, limit: int):
    """`blame -n 0` on a file a turn *did* touch used to print "no recorded turn touched
    src/app.py" — a false statement about the record, produced by a usage error."""
    write(tmp_path, turn(path="src/app.py"))
    assert log(tmp_path, limit=limit) == []
    said = blame(tmp_path, "src/app.py", limit=limit)[0]
    assert "-n wants a positive number" in said
    assert "no recorded turn touched" not in said


def test_a_pathful_query_does_not_match_a_vendored_copy_of_the_same_path(tmp_path: Path):
    """A turn that only touched `vendor/src/app.py` must not be reported as having touched
    `src/app.py`. Suffix matching belongs to bare basenames, not to paths."""
    write(tmp_path, turn(path="vendor/src/app.py", claim="Patched the vendored copy"))

    lines = blame(tmp_path, "src/app.py")

    assert lines[0].startswith("tycho: no recorded turn touched src/app.py")
    assert "vendored" not in "\n".join(lines)
    # The file that *was* touched still answers for itself.
    assert blame(tmp_path, "vendor/src/app.py")[0].startswith("vendor/src/app.py — 1 turn")


def test_a_bare_basename_says_which_file_each_row_is_about(tmp_path: Path):
    """Two files named `app.py` are two files. Printing "app.py — 2 turns" with no paths
    reads as one file with a history it doesn't have."""
    write(tmp_path,
          turn(path="lib/app.py", id="1" * 16, claim="lib work"),
          turn(path="vendor/src/app.py", id="2" * 16, claim="vendor work"))

    lines = blame(tmp_path, "app.py")

    assert lines[0].startswith("app.py — 2 turns")
    assert "matched 2 files" in lines[1]
    body = "\n".join(lines)
    assert "lib/app.py" in body and "vendor/src/app.py" in body


def test_blame_on_a_large_record_file_stays_bounded(tmp_path: Path):
    """5000 turns, ten rows: `record.touching` streams and holds `limit` records, so the
    cost of a blame is the size of its answer, not the size of the history."""
    write(tmp_path, *(turn(id=f"{i:016x}", ended_at=NOW - i) for i in range(5000)))
    started = time.monotonic()
    lines = blame(tmp_path, "src/app.py", limit=10)
    assert len(lines) == 1 + 2 * 10  # header + two lines per turn
    assert time.monotonic() - started < 10  # generous: it's the O(1) memory that matters
    assert "5000 turns" not in lines[0] and lines[0].startswith("src/app.py — 10 turns")


def test_log_filters_inside_the_stream_not_after_it(tmp_path: Path):
    """`log --verdict FAILED -n 3` must be three failures, not the failures among three
    turns — a post-filter silently answers a different question."""
    records = []
    for i in range(30):
        records.append(turn(id=f"{i:016x}", ended_at=NOW - 30 + i,
                            verdict="FAILED" if i % 10 == 0 else "VERIFIED"))
    write(tmp_path, *records)
    assert len(log(tmp_path, limit=3, verdict="FAILED")) == 3


# --- filters -----------------------------------------------------------------


def test_verdict_filter_is_case_insensitive(tmp_path: Path):
    write(tmp_path, turn(verdict="FAILED", claim="broke it"), turn(verdict="VERIFIED"))
    assert len(log(tmp_path, verdict="failed")) == 1
    assert '"broke it"' in log(tmp_path, verdict="failed")[0]


def test_since_keeps_only_turns_on_or_after_that_day(tmp_path: Path):
    day = 86400
    write(tmp_path,
          turn(claim="old", ended_at=NOW - 10 * day, id="a" * 16),
          turn(claim="recent", ended_at=NOW, id="b" * 16))
    lines = log(tmp_path, since=time.strftime("%Y-%m-%d", time.localtime(NOW - day)))
    assert len(lines) == 1 and '"recent"' in lines[0]


def test_a_junk_since_is_rejected_rather_than_ignored(tmp_path: Path):
    """Silently ignoring it would show unfiltered history under a filter's name."""
    write(tmp_path, turn())
    assert log(tmp_path, since="last tuesday")[0].startswith("tycho log: --since wants a date")


def test_filters_combine(tmp_path: Path):
    write(tmp_path,
          turn(verdict="FAILED", ended_at=NOW - 10 * 86400, id="a" * 16),
          turn(verdict="FAILED", ended_at=NOW, id="b" * 16, claim="today's failure"),
          turn(verdict="VERIFIED", ended_at=NOW, id="c" * 16))
    lines = log(tmp_path, verdict="FAILED",
                since=time.strftime("%Y-%m-%d", time.localtime(NOW - 86400)))
    assert len(lines) == 1 and "today's failure" in lines[0]


# --- corrupt, partial, and nullable input ------------------------------------


def test_a_missing_record_file_is_an_empty_history_not_an_error(tmp_path: Path):
    assert "no turns recorded" in log(tmp_path)[0]
    assert "no turns recorded" in blame(tmp_path, "src/app.py")[0]


def test_null_model_and_session_render_as_a_state_not_a_crash(tmp_path: Path):
    """Both are nullable by design — `record.py` never guesses attribution."""
    write(tmp_path, turn(model=None, session=None))
    assert "model unknown" in blame(tmp_path, "src/app.py")[1]


def test_a_record_missing_every_optional_field_still_renders(tmp_path: Path):
    write(tmp_path, {"schema": 1, "id": "ff" * 8,
                     "files": [{"path": "src/app.py", "kind": "edit", "ts": NOW}]})
    lines = blame(tmp_path, "src/app.py")
    assert len(lines) == 3
    assert "?" in lines[1]  # unknown when, unknown verdict — said, not invented
    assert "(no claim recorded)" in lines[2]


def test_wrong_typed_fields_do_not_raise(tmp_path: Path):
    write(tmp_path, {"schema": 1, "id": 12345, "verdict": ["FAILED"], "stage": None,
                     "ended_at": "yesterday", "checks": "nope", "commands": {"a": 1},
                     "claims": "a string, not a list",
                     "files": [{"path": "src/app.py", "kind": "edit", "ts": 1}]})
    assert len(blame(tmp_path, "src/app.py")) == 3
    assert len(log(tmp_path)) == 1


def test_a_dotfile_path_is_blameable(tmp_path: Path):
    """`record.touching` normalizes with `lstrip("./")`, which strips a character *set*:
    `.github/…` becomes `github/…` and matches nothing. This surface must not inherit that."""
    write(tmp_path, turn(path=".github/workflows/ci.yml"))
    assert blame(tmp_path, ".github/workflows/ci.yml")[0].startswith(
        ".github/workflows/ci.yml — 1 turn")
    assert blame(tmp_path, "./.github/workflows/ci.yml")[0].startswith(
        ".github/workflows/ci.yml — 1 turn")


def test_a_bare_dotfile_basename_still_matches(tmp_path: Path):
    """The header echoes the query, not the match — a basename can hit several files."""
    write(tmp_path, turn(path="src/.env.example"))
    assert blame(tmp_path, ".env.example")[0].startswith(".env.example — 1 turn")


def test_a_file_entry_that_is_not_a_dict_is_skipped(tmp_path: Path):
    write(tmp_path, {"schema": 1, "id": "ab" * 8, "files": ["src/app.py", None]})
    assert blame(tmp_path, "src/app.py")[0].startswith("tycho: no recorded turn touched")


# --- redaction is upstream, and stays upstream -------------------------------


def test_what_the_record_redacted_stays_redacted_on_screen(tmp_path: Path):
    """§10: `log` makes durable, greppable history. `record.py` redacts on write; nothing
    here may reconstruct a field from anywhere else."""
    secret = "ghp_" + "a" * 36
    write(tmp_path, turn(
        claim=record.redact(f"exported GITHUB_TOKEN={secret} and pushed"),
        commands=[{"cmd": record.redact(f"curl -H 'Authorization: Bearer {secret}'"),
                   "runner": True, "outcome": "failed"}]))
    rendered = "\n".join(blame(tmp_path, "src/app.py") + log(tmp_path))
    assert secret not in rendered
    assert "[REDACTED]" in rendered


# --- output shape (a formatting regression should fail a test) ---------------


def test_blame_is_two_aligned_lines_per_turn(tmp_path: Path):
    write(tmp_path,
          turn(id="a" * 16, ended_at=NOW - 3600, verdict="VERIFIED", claim="added backoff"),
          turn(id="b" * 16, ended_at=NOW - 30, verdict="INDETERMINATE",
               stage="artifact_changed", claim="fixed the retry logic",
               checks=[{"name": "command_execution", "status": "UNSUPPORTED",
                        "evidence": "no test/build command ran this turn"}], commands=[]))
    assert blame(tmp_path, "src/app.py:42") == [
        "src/app.py — 2 turns, newest first",
        "  note: asked for :42 — attribution is file-level. Tycho records which turns "
        "touched a file, not which lines.",
        "  just now  INDETERMINATE  artifact_changed  turn bbbbbbbb  claude-opus-5",
        '    "fixed the retry logic" — never verified — no check could conclude',
        "  1h ago    VERIFIED       claim_supported   turn aaaaaaaa  claude-opus-5",
        '    "added backoff" — pytest -q passed',
    ]


def test_log_is_one_aligned_line_per_turn(tmp_path: Path):
    write(tmp_path,
          turn(id="a" * 16, ended_at=NOW - 3600, claim="added backoff"),
          turn(id="b" * 16, ended_at=NOW - 30, verdict="FAILED", stage="executed",
               path=["src/app.py", "README.md"], claim="fixed the retry logic"))
    assert log(tmp_path) == [
        'just now  bbbbbbbb  FAILED    executed         2 files  "fixed the retry logic"',
        '1h ago    aaaaaaaa  VERIFIED  claim_supported  1 file   "added backoff"',
    ]


def test_columns_are_sized_to_what_is_printed(tmp_path: Path):
    """Padding every verdict to INDETERMINATE costs five dead columns on a screen where
    every verdict is VERIFIED — and the claim is what pays for them."""
    write(tmp_path, turn(id="a" * 16))
    assert log(tmp_path) == [
        '1h ago  aaaaaaaa  VERIFIED  claim_supported  1 file  "fixed the retry logic"',
    ]


def test_the_empty_states_fit_the_width_budget(tmp_path: Path):
    """They are the longest prose Tycho prints here — the easiest thing to let sprawl."""
    empty = log(tmp_path) + blame(tmp_path, "src/app.py")
    write(tmp_path, turn(path="src/other.py"))
    for line in empty + blame(tmp_path, "src/app.py") + [archaeology._WHY_EMPTY]:
        assert len(line) <= archaeology._WIDTH, line


def test_every_line_fits_the_width_budget(tmp_path: Path):
    write(tmp_path, turn(
        claim="A really quite long summary of everything that was done this turn " * 4,
        checks=[{"name": "git_state", "status": "FAIL",
                 "evidence": "claimed edits absent from repo: " + ", ".join(
                     f"src/module_{i}.py" for i in range(20))}]))
    for line in blame(tmp_path, "src/app.py") + log(tmp_path):
        assert len(line) <= archaeology._WIDTH, line


@pytest.mark.parametrize("delta,expected", [
    (0, "just now"),
    (30, "just now"),
    (-500, "just now"),        # a skewed clock reads as now, never as the future
    (60, "1m ago"),
    (3600 * 5, "5h ago"),
    (86400 * 3, "3d ago"),
])
def test_relative_time_while_relative_is_useful(delta: float, expected: str):
    assert archaeology._ago(NOW - delta, NOW) == expected


def test_older_than_a_week_gets_a_date():
    """Past the debugging week a date is what someone actually correlates against."""
    anchor = datetime(2026, 7, 27, 12, 0).timestamp()
    assert archaeology._ago(datetime(2026, 7, 4, 9, 0).timestamp(), anchor) == "Jul 04"


def test_a_different_year_never_reads_as_this_one():
    """A bare "Dec 26" silently means a different year — say the year."""
    anchor = datetime(2027, 1, 15, 12, 0).timestamp()
    assert archaeology._ago(datetime(2026, 12, 26, 9, 0).timestamp(), anchor) == "2026-12-26"


def test_a_missing_timestamp_reads_as_unknown():
    assert archaeology._ago(None, NOW) == "?"
    assert archaeology._ago(0, NOW) == "?"


def test_singular_and_plural_counts(tmp_path: Path):
    write(tmp_path, turn(path=["a.py", "b.py"]), turn(path=["a.py"], id="b" * 16))
    assert "1 file" in log(tmp_path)[0]
    assert "2 files" in log(tmp_path)[1]
    assert blame(tmp_path, "a.py")[0].startswith("a.py — 2 turns")


def test_a_turn_that_touched_nothing_says_no_files(tmp_path: Path):
    rec = turn()
    rec["files"] = []
    write(tmp_path, rec)
    assert "no files" in log(tmp_path)[0]


# --- colour ------------------------------------------------------------------


def test_no_colour_to_a_pipe(tmp_path: Path, monkeypatch, capsys):
    """Escape codes in a `| grep` are worse than no colour at all."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    write(tmp_path, turn())
    with capsys.disabled():  # capsys leaves a non-tty stdout in place either way
        assert "\033[" not in "".join(log(tmp_path))


def test_colour_on_a_tty_uses_the_status_palette(tmp_path: Path, monkeypatch):
    """Same verdict, same colour, wherever Tycho draws it."""
    from tycho.wire import status

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(archaeology, "_colour", lambda: True)
    write(tmp_path, turn(verdict="FAILED"))
    assert status._VERDICT_COLOUR["FAILED"] in log(tmp_path)[0]


def test_colour_does_not_break_alignment(tmp_path: Path, monkeypatch):
    """`len` counts escape codes; the columns must not."""
    monkeypatch.setattr(archaeology, "_colour", lambda: True)
    write(tmp_path, turn(id="a" * 16, verdict="FAILED"),
          turn(id="b" * 16, verdict="VERIFIED", ended_at=NOW - 60))
    plain = [archaeology._ANSI.sub("", line) for line in log(tmp_path)]
    assert plain[0].index("claim_supported") == plain[1].index("claim_supported")


def test_no_color_env_wins_over_a_tty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdout", type("T", (), {"isatty": lambda self: True})())
    assert archaeology._colour() is False

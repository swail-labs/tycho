"""`tycho count` + the catch record `catches.json`.

catches.json holds what Tycho caught — the running tally *and* the evidence trail (which
checks failed or couldn't pass), newest first. Every adverse/intermediate run is recorded
(no transition dedup): "hold ALL the failed and intermediate runs".
"""

from __future__ import annotations

import json
from pathlib import Path

from tycho import cli, state
from tycho.model import CheckResult, CheckStatus

ZERO = {"FAILED": 0, "STALE": 0, "INDETERMINATE": 0}


def _results(*specs):
    """(name, status-name, evidence) → CheckResults."""
    return [CheckResult(n, CheckStatus[s], e) for n, s, e in specs]


FAIL_RUN = (("command_execution", "FAIL", "pytest exited 1"), ("file_state", "PASS", "ok"))


# --- the tally ---------------------------------------------------------------

def test_adverse_verdict_tallies_here_and_all_time(tmp_path: Path):
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    assert state.counts(tmp_path) == {**ZERO, "FAILED": 1}
    assert state.all_time_counts() == {**ZERO, "FAILED": 1}


def test_stale_and_indeterminate_count_too(tmp_path: Path):
    state.record_catch(tmp_path, "claude", "STALE", _results(("test_freshness", "STALE", "x")))
    state.record_catch(tmp_path, "claude", "INDETERMINATE", _results(("git_state", "UNSUPPORTED", "x")))
    assert state.counts(tmp_path) == {"FAILED": 0, "STALE": 1, "INDETERMINATE": 1}


def test_clean_verdicts_count_as_runs_but_are_not_catches(tmp_path: Path):
    # VERIFIED/UNSUPPORTED are still not catches (no adverse count, no evidence) —
    # but they ARE runs, and UNSUPPORTED is blind (Tycho had nothing to say).
    for verdict in ("VERIFIED", "UNSUPPORTED"):
        state.record_catch(tmp_path, "claude", verdict, _results(("x", "PASS", "ok")))
    assert state.counts(tmp_path) == ZERO
    assert state.catches(tmp_path) == []
    assert state.totals(tmp_path) == {"runs": 2, "blind": 1}  # both ran; UNSUPPORTED is blind


def test_totals_track_the_denominator_and_blind_spot(tmp_path: Path):
    # runs is every verdict recorded; blind is INDETERMINATE + UNSUPPORTED.
    for v in ("VERIFIED", "VERIFIED", "FAILED", "STALE", "INDETERMINATE", "UNSUPPORTED"):
        state.record_catch(tmp_path, "claude", v, _results(("x", "PASS", "ok")))
    assert state.totals(tmp_path) == {"runs": 6, "blind": 2}
    assert state.counts(tmp_path) == {"FAILED": 1, "STALE": 1, "INDETERMINATE": 1}


def test_every_adverse_run_is_recorded_no_dedup(tmp_path: Path):
    # hold ALL — a standing failure across three turns is three entries, not one.
    for _ in range(3):
        state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    assert state.counts(tmp_path)["FAILED"] == 3
    assert len(state.catches(tmp_path)) == 3


# --- the evidence trail ------------------------------------------------------

def test_catches_hold_the_evidence_trail_newest_first(tmp_path: Path):
    state.record_catch(tmp_path, "claude", "STALE", _results(("test_freshness", "STALE", "old")))
    state.record_catch(tmp_path, "claude", "FAILED",
                       _results(("command_execution", "FAIL", "boom"), ("file_state", "PASS", "fine")))
    trail = state.catches(tmp_path)
    assert [c["verdict"] for c in trail] == ["FAILED", "STALE"]  # newest first
    # only the non-PASS checks land in the trail, with status + the report's evidence
    assert trail[0]["checks"] == [{"check": "command_execution", "status": "FAIL", "evidence": "boom"}]
    assert set(trail[0]) == {"at", "harness", "verdict", "checks"}


def test_the_list_is_bounded_but_the_tally_is_exact(tmp_path: Path):
    for _ in range(state._CATCH_LIST_CAP + 5):
        state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    assert len(state.catches(tmp_path)) == state._CATCH_LIST_CAP  # recent-N trail
    assert state.counts(tmp_path)["FAILED"] == state._CATCH_LIST_CAP + 5  # tally is complete


def test_no_last_run_or_heartbeat_info_lives_in_catches(tmp_path: Path):
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    data = json.loads((state.dir_for(tmp_path) / "catches.json").read_text())
    assert set(data) == {"tally", "catches"}
    assert "last" not in data  # the latest verdict lives in last-run.json, not here


def test_machine_tally_has_no_cross_repo_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "home"))
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    machine = json.loads((state.user_dir() / "catches.json").read_text())
    assert set(machine) == {"tally"}  # running total only, no evidence trail


def test_all_time_accumulates_across_repos(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "home"))
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    state.record_catch(one, "claude", "FAILED", _results(*FAIL_RUN))
    state.record_catch(two, "codex", "STALE", _results(("test_freshness", "STALE", "x")))
    assert state.counts(one) == {**ZERO, "FAILED": 1}
    assert state.counts(two) == {**ZERO, "STALE": 1}
    assert state.all_time_counts() == {"FAILED": 1, "STALE": 1, "INDETERMINATE": 0}


# --- migration from the old counts.json --------------------------------------

def test_migrates_a_legacy_counts_file(tmp_path: Path):
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "counts.json").write_text('{"last": "FAILED", "FAILED": 3, "STALE": 1}')
    assert state.counts(tmp_path) == {"FAILED": 3, "STALE": 1, "INDETERMINATE": 0}  # numbers carry over
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))  # re-homes into catches.json
    assert state.counts(tmp_path)["FAILED"] == 4
    assert (state.dir_for(tmp_path) / "catches.json").exists()
    assert not (state.dir_for(tmp_path) / "counts.json").exists()  # legacy file dropped


# --- fail-open ---------------------------------------------------------------

def test_counts_and_catches_are_empty_before_anything_is_recorded(tmp_path: Path):
    assert state.counts(tmp_path) == ZERO
    assert state.catches(tmp_path) == []


def test_corrupt_catches_file_reads_as_zero(tmp_path: Path):
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "catches.json").write_text("{not json")
    assert state.counts(tmp_path) == ZERO


def test_garbage_tally_values_read_as_zero(tmp_path: Path):
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "catches.json").write_text('{"tally": {"FAILED": "lots", "STALE": -4}}')
    assert state.counts(tmp_path) == ZERO


def _boom(*_args, **_kwargs):
    raise OSError("disk is on fire")


def test_record_catch_never_raises_when_it_cannot_write(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_write_json", _boom)
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))  # must not raise


def test_all_time_tally_never_lands_inside_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    state.record_catch(repo, "claude", "FAILED", _results(*FAIL_RUN))
    assert state.all_time_counts() == {**ZERO, "FAILED": 1}
    assert (tmp_path / "home" / "catches.json").is_file()  # the machine-level tally...
    assert state.user_dir() not in repo.parents  # ...and never under someone's repo


# --- the command -------------------------------------------------------------

def test_count_command_reports_both_scopes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    # A *separate* repo, so its catch counts toward all-time but not this one. The `.git` is
    # what makes it separate: state resolution walks up until a git root, so without
    # the marker `other` is just a subdirectory of tmp_path and its catch lands in tmp_path.
    (other / ".git").mkdir()
    state.record_catch(tmp_path, "claude", "FAILED", _results(*FAIL_RUN))
    state.record_catch(tmp_path, "claude", "STALE", _results(("test_freshness", "STALE", "x")))
    state.record_catch(tmp_path, "claude", "INDETERMINATE", _results(("git_state", "UNSUPPORTED", "x")))
    state.record_catch(other, "codex", "FAILED", _results(*FAIL_RUN))

    assert cli.main(["count"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    # catches read against a denominator; INDETERMINATE now folds into "blind".
    # runs + blind rate lead — blind is the metric that doesn't decay (§7).
    assert "this repo: 3 runs, 1 blind (33%), 2 caught (1 FAILED, 1 STALE)" in out
    assert "all-time: 4 runs, 1 blind (25%), 3 caught (2 FAILED, 1 STALE)" in out


def test_count_command_on_a_quiet_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["count"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "this repo: 0 caught" in out
    assert "all-time: 0 caught" in out


# --- where the machine-level tally lives ------------------------------------

def test_user_dir_honors_tycho_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path / "relocated"))
    assert state.user_dir() == tmp_path / "relocated"


def test_user_dir_falls_back_to_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("TYCHO_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert state.user_dir() == tmp_path / "xdg" / "tycho"


def test_user_dir_defaults_under_home(tmp_path, monkeypatch):
    monkeypatch.delenv("TYCHO_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(state.Path, "home", lambda: tmp_path)
    assert state.user_dir() == tmp_path / ".local" / "share" / "tycho"


def test_user_dir_ignores_empty_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", "")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert state.user_dir() == tmp_path / "xdg" / "tycho"


def test_user_dir_expands_user_in_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows reads this one
    monkeypatch.setenv("TYCHO_HOME", "~/relocated")
    assert state.user_dir() == tmp_path / "relocated"

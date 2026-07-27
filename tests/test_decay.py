"""The decay ledger (strategy §7/§9.5): per-check, per-model catch and blind rates.

The point of the ledger is deciding what to *retire*, so these tests pin the denominators
harder than the formatting: a rate whose denominator drifts is worse than no rate at all.

- catch rate = caught / **spoke** (turns the check reached PASS|FAIL|STALE)
- blind rate = blind / **seen** (every turn the check ran in, spoke + blind)

Also pinned: nothing here leaves the machine, a legacy `catches.json` with no attribution
still renders, a null model id is its own bucket (never guessed, never merged), and the
whole thing fails open the way every other reader in `state.py` does.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_record import make_session
from tycho import cli, record, state
from tycho.model import Attribution, CheckResult, CheckStatus, Verdict


def turn(model="claude-opus-5", agent_version="2.1.220", harness="claude",
         verdict="VERIFIED", checks=(), started_at=1000.0, ended_at=1001.0) -> dict:
    """One hand-built turn record — the shape `record.build` writes, minus the fields the
    ledger never reads. Hand-built on purpose: the ledger's contract is with the *file*."""
    return {
        "schema": record.SCHEMA, "id": "0" * 16, "session": "s", "harness": harness,
        "model": model, "agent_version": agent_version,
        "started_at": started_at, "ended_at": ended_at,
        "verdict": verdict, "stage": "claim_supported",
        "checks": [{"name": n, "status": s, "evidence": ""} for n, s in checks],
        "files": [], "commands": [], "claims": [],
    }


def write(repo: Path, *records: dict) -> None:
    for r in records:
        assert record.append(repo, r)


# --- attribution -------------------------------------------------------------

def test_attribution_is_stamped_and_read_back(tmp_path: Path):
    write(tmp_path, turn(), turn(model="claude-sonnet-4", agent_version="2.0.1"))
    models = state.ledger(tmp_path)["models"]
    assert [(m["model"], m["agent_version"], m["harness"], m["turns"]) for m in models] == [
        ("claude-opus-5", "2.1.220", "claude", 1),
        ("claude-sonnet-4", "2.0.1", "claude", 1),
    ]


def test_attribution_survives_the_real_build_path(tmp_path: Path):
    # Not a hand-built dict: a gathered Session → record.build → append → ledger, so a rename
    # of Attribution.model would fail here rather than silently emptying the ledger.
    session = make_session(attribution=Attribution("claude-opus-6", "3.0.0", "sess-9"))
    results = [CheckResult("file_state", CheckStatus.FAIL, "no such file")]
    write(tmp_path, record.build(session, results, Verdict.FAILED, "claude", 200.0))
    ledger = state.ledger(tmp_path)
    assert ledger["models"][0]["model"] == "claude-opus-6"
    assert ledger["models"][0]["agent_version"] == "3.0.0"
    assert ledger["caught"] == 1


def test_a_null_model_is_its_own_bucket_never_merged(tmp_path: Path):
    # A harness that doesn't expose a model id is a fact about the evidence. Folding those
    # turns into a neighbouring model would corrupt the exact measurement this exists for.
    write(tmp_path, turn(), turn(model=None, agent_version=None, harness="codex"))
    models = state.ledger(tmp_path)["models"]
    assert {m["model"] for m in models} == {"claude-opus-5", None}
    assert cli._model_label({"model": None, "harness": "codex"}) == "unknown (codex)"


def test_harness_separates_buckets_even_for_one_model(tmp_path: Path):
    write(tmp_path, turn(harness="claude"), turn(harness="cursor"))
    assert len(state.ledger(tmp_path)["models"]) == 2


# --- run-level rates ---------------------------------------------------------

def test_run_level_caught_and_blind(tmp_path: Path):
    for v in ("VERIFIED", "FAILED", "STALE", "INDETERMINATE", "UNSUPPORTED", "OVERRIDDEN"):
        write(tmp_path, turn(verdict=v))
    ledger = state.ledger(tmp_path)
    assert ledger["turns"] == 6
    assert ledger["caught"] == 2  # FAILED + STALE
    assert ledger["blind"] == 2  # INDETERMINATE + UNSUPPORTED — OVERRIDDEN is neither
    assert (ledger["first"], ledger["last"]) == (1000.0, 1001.0)


def test_blind_rate_is_reported_per_model(tmp_path: Path):
    write(tmp_path,
          turn(model="old", verdict="UNSUPPORTED"), turn(model="old", verdict="FAILED"),
          turn(model="new", verdict="VERIFIED"), turn(model="new", verdict="INDETERMINATE"))
    by_model = {m["model"]: m for m in state.ledger(tmp_path)["models"]}
    assert (by_model["old"]["caught"], by_model["old"]["blind"]) == (1, 1)
    assert (by_model["new"]["caught"], by_model["new"]["blind"]) == (0, 1)


# --- per-check denominators (the whole point) --------------------------------

def test_spoke_excludes_the_turns_a_check_could_not_speak_to(tmp_path: Path):
    # 10 turns: the check is UNSUPPORTED on 8, PASSes on 1, FAILs on 1. Catch rate is 1/2
    # (50%) over the turns it could speak to — NOT 1/10 — and the 80% blind rate sits beside
    # it so nobody reads the 50% as "this check catches half of everything".
    for _ in range(8):
        write(tmp_path, turn(checks=(("command_execution", "UNSUPPORTED"),)))
    write(tmp_path, turn(checks=(("command_execution", "PASS"),)))
    write(tmp_path, turn(verdict="FAILED", checks=(("command_execution", "FAIL"),)))
    check = state.ledger(tmp_path)["checks"][0]
    assert (check["spoke"], check["caught"], check["blind"]) == (2, 1, 8)
    assert cli._rate(check["caught"], check["spoke"]) == "1 (50%)"
    assert cli._rate(check["blind"], check["spoke"] + check["blind"]) == "8 (80%)"


def test_stale_counts_as_a_catch_and_indeterminate_as_blind(tmp_path: Path):
    write(tmp_path, turn(verdict="STALE", checks=(("test_freshness", "STALE"),
                                                  ("git_state", "INDETERMINATE"))))
    checks = {c["name"]: c for c in state.ledger(tmp_path)["checks"]}
    assert (checks["test_freshness"]["caught"], checks["test_freshness"]["spoke"]) == (1, 1)
    assert (checks["git_state"]["caught"], checks["git_state"]["blind"]) == (0, 1)


def test_per_check_slices_by_model_the_retirement_signal(tmp_path: Path):
    # The §7 signal: file_state caught things on the old model and nothing on the new one.
    for _ in range(2):
        write(tmp_path, turn(model="gen-1", verdict="FAILED", checks=(("file_state", "FAIL"),)))
    for _ in range(3):
        write(tmp_path, turn(model="gen-2", checks=(("file_state", "PASS"),)))
    check = state.ledger(tmp_path)["checks"][0]
    assert check["models"] == [
        {"model": "gen-2", "spoke": 3, "caught": 0},  # most turns first
        {"model": "gen-1", "spoke": 2, "caught": 2},
    ]


def test_checks_are_sorted_by_name_for_a_stable_render(tmp_path: Path):
    write(tmp_path, turn(checks=(("scope_drift", "PASS"), ("command_execution", "PASS"))))
    assert [c["name"] for c in state.ledger(tmp_path)["checks"]] == [
        "command_execution", "scope_drift"]


# --- the headline ------------------------------------------------------------

def test_count_leads_with_runs_and_blind_rate(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for v in ("VERIFIED", "FAILED", "UNSUPPORTED", "INDETERMINATE"):
        state.record_catch(tmp_path, "claude", v, [])
    assert cli.main(["count"]) == cli.ExitCode.OK
    # blind is promoted out of its trailing clause: it's the metric that doesn't decay (§7).
    assert "this repo: 4 runs, 2 blind (50%), 1 caught (1 FAILED)" in capsys.readouterr().out


def test_blind_rate_is_shown_even_at_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    state.record_catch(tmp_path, "claude", "VERIFIED", [])
    assert cli.main(["count"]) == cli.ExitCode.OK
    assert "this repo: 1 run, 0 blind (0%), 0 caught" in capsys.readouterr().out


# --- the ledger view ---------------------------------------------------------

def test_ledger_view_renders_models_checks_and_its_own_denominators(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path,
          turn(model="gen-1", verdict="FAILED",
               checks=(("file_state", "FAIL"), ("command_execution", "UNSUPPORTED"))),
          turn(model="gen-2", checks=(("file_state", "PASS"), ("command_execution", "PASS"))))
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "ledger: 2 turns on the record" in out
    assert "gen-1" in out and "gen-2" in out
    assert "file_state" in out and "gen-1 1/1" in out  # caught/spoke, per model
    assert "command_execution" in out and "1 (50%)" in out  # blind over seen (1 of 2)
    assert "catch rate = caught / turns the check could speak to" in out
    assert "retirement signal" in out


def test_bare_count_does_not_print_the_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, turn())
    assert cli.main(["count"]) == cli.ExitCode.OK
    assert "ledger" not in capsys.readouterr().out


def test_ledger_view_says_so_when_there_is_no_record(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    assert "no turns recorded here yet" in capsys.readouterr().out


def test_empty_denominator_never_renders_as_zero_percent():
    # A check that has never spoken has no catch rate. "0 (0%)" would read as "it looked and
    # found nothing", which is exactly the wrong conclusion to draw about a check to retire.
    assert cli._rate(0, 0) == "0 (—)"
    assert cli._pct(0, 0) == "—"


# --- backwards compatibility: deployed installs ------------------------------

def test_legacy_catches_file_with_no_attribution_still_counts(tmp_path, monkeypatch, capsys):
    # A pre-TYCHO-131 install: a tally, an evidence trail, no attribution, no turns.jsonl.
    monkeypatch.chdir(tmp_path)
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "catches.json").write_text(json.dumps({
        "tally": {"runs": 20, "FAILED": 3, "STALE": 1, "UNSUPPORTED": 2, "VERIFIED": 14},
        "catches": [{"at": 1.0, "harness": "claude", "verdict": "FAILED", "checks": []}],
    }))
    assert state.counts(tmp_path) == {"FAILED": 3, "STALE": 1, "INDETERMINATE": 0}
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "this repo: 20 runs, 2 blind (10%), 4 caught (3 FAILED, 1 STALE)" in out
    assert "no turns recorded here yet" in out  # honest: no record, so no ledger


def test_legacy_tally_with_no_run_count_keeps_the_bare_form(tmp_path, monkeypatch, capsys):
    # Older still (pre-TYCHO-58): no denominator at all. It must not become "0 runs, 0 blind".
    monkeypatch.chdir(tmp_path)
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "counts.json").write_text('{"FAILED": 3, "STALE": 1}')
    assert cli.main(["count"]) == cli.ExitCode.OK
    assert "this repo: 4 caught (3 FAILED, 1 STALE)" in capsys.readouterr().out


def test_recording_after_a_legacy_file_keeps_both_surfaces_working(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    state.dir_for(tmp_path).mkdir()
    (state.dir_for(tmp_path) / "counts.json").write_text('{"FAILED": 3}')
    state.record_catch(tmp_path, "claude", "FAILED", [])
    write(tmp_path, turn(verdict="FAILED", checks=(("file_state", "FAIL"),)))
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "4 caught (4 FAILED)" in out  # legacy tally carried across, new run added
    assert "ledger: 1 turn on the record" in out


# --- fail-open ---------------------------------------------------------------

def test_ledger_is_empty_before_anything_is_recorded(tmp_path: Path):
    assert state.ledger(tmp_path) == {
        "turns": 0, "first": None, "last": None, "caught": 0, "blind": 0,
        "models": [], "checks": [],
    }


def test_corrupt_and_partial_lines_are_skipped_not_fatal(tmp_path: Path):
    path = record.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{not json\n"
        '"a bare string"\n'
        "[1, 2, 3]\n"
        + json.dumps({"no": "verdict"}) + "\n"
        + json.dumps(turn(verdict="FAILED", checks=(("file_state", "FAIL"),))) + "\n"
        + '{"verdict": "VERIFIED", "checks": "not a list", "started_at": "soon"}\n'
        + '{"verdict": "VERIFIED", "checks": [{"status": "PASS"}, 7]}\n',
        encoding="utf-8",
    )
    ledger = state.ledger(tmp_path)
    assert ledger["turns"] == 3  # the three rows carrying a verdict
    assert ledger["caught"] == 1
    assert [c["name"] for c in ledger["checks"]] == ["file_state"]  # nameless entries dropped
    assert ledger["first"] == 1000.0  # the string timestamp was ignored, not parsed


def test_ledger_never_raises_on_an_unreadable_record(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(record, "path_for", lambda repo: tmp_path)  # a directory, not a file
    assert state.ledger(tmp_path)["turns"] == 0


def test_ledger_view_never_raises_on_a_garbage_record(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = record.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00\x01garbage\n", encoding="utf-8")
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    assert "no turns recorded here yet" in capsys.readouterr().out


def test_ledger_reads_state_from_the_repo_root_not_a_subdirectory(tmp_path, monkeypatch, capsys):
    # Same TYCHO-79 walk as every other reader: run from src/, still see the repo's record.
    (tmp_path / ".git").mkdir()
    write(tmp_path, turn())
    sub = tmp_path / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert cli.main(["count", "--ledger"]) == cli.ExitCode.OK
    assert "ledger: 1 turn on the record" in capsys.readouterr().out


def test_the_ledger_opens_no_socket(tmp_path, monkeypatch):
    # §8/§10: nothing about measuring decay may become telemetry. The ledger is one local
    # file read, and this is the test that keeps it that way.
    import socket

    def _no(*_a, **_k):
        raise AssertionError("the decay ledger must never open a socket")

    monkeypatch.setattr(socket, "socket", _no)
    monkeypatch.setattr(socket, "create_connection", _no)
    write(tmp_path, turn())
    assert state.ledger(tmp_path)["turns"] == 1
    assert cli._ledger_lines(state.ledger(tmp_path))

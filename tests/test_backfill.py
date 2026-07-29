"""`tycho backfill` — seeding the record from transcripts written before Tycho existed here.

The load-bearing property is not "it writes rows", it is **what those rows claim**. A
backfilled turn was never checked, so it must not read as a verdict, must not move the decay
ledger, and must not be written twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import turn_record
from tycho.read import harness as harness_mod
from tycho.store import record as record_mod
from tycho.store import state
from tycho.wire import backfill

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_multiturn.jsonl"


@pytest.fixture
def repo_with_history(git_repo: Path, monkeypatch, tmp_path: Path) -> Path:
    """A repo whose Claude Code transcript directory holds one two-turn session."""
    claude_home = tmp_path / "claude-home"
    projects = claude_home / "projects" / str(git_repo).translate(
        str.maketrans({c: "-" for c in "\\/:. "})
    )
    projects.mkdir(parents=True)
    (projects / "session-a.jsonl").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", str(claude_home))
    state.dir_for(git_repo).mkdir(parents=True, exist_ok=True)
    return git_repo


def rows(repo: Path) -> list[dict]:
    return list(record_mod.iter_records(repo))


# --- it finds and cuts the history ------------------------------------------


def test_history_enumerates_every_transcript_not_just_the_newest(repo_with_history: Path):
    found = harness_mod.CLAUDE.history(repo_with_history)
    assert [p.name for p in found] == ["session-a.jsonl"]


def test_a_transcript_is_cut_into_its_turns(repo_with_history: Path):
    result = backfill.run(repo_with_history)
    assert result["sessions"] == 1
    assert result["turns"] == 2, "the fixture holds two user turns"


def test_the_turns_carry_what_the_transcript_proves(repo_with_history: Path):
    backfill.run(repo_with_history)
    first, second = rows(repo_with_history)
    assert [f["path"] for f in first["files"]] == ["app.py"]
    assert "Added `greet()` to app.py." in " ".join(first["claims"])
    assert second["files"] == [], "the second turn only read the file back"
    assert first["started_at"] < second["started_at"]


def test_attribution_is_carried_never_guessed(repo_with_history: Path):
    backfill.run(repo_with_history)
    for row in rows(repo_with_history):
        assert row["harness"] == "claude"
        # The fixture carries no model/version; a backfilled row stores the null, not a guess.
        assert row["model"] is None
        assert row["agent_version"] is None


# --- what the rows must NOT claim -------------------------------------------


def test_a_backfilled_turn_carries_no_verdict_and_no_checks(repo_with_history: Path):
    backfill.run(repo_with_history)
    for row in rows(repo_with_history):
        assert row["verdict"] == "UNVERIFIED"
        assert row["checks"] == []
        assert row["backfilled"] is True


def test_the_stage_never_claims_more_than_the_transcript_shows(repo_with_history: Path):
    """`artifact_changed` and `claim_supported` are statements about state that no longer
    exists. A replayed turn can only honestly report whether a runner ran."""
    backfill.run(repo_with_history)
    assert {row["stage"] for row in rows(repo_with_history)} <= {"attempted", "executed"}


def test_backfilled_turns_do_not_move_the_decay_ledger(repo_with_history: Path):
    """Catch rate and blind rate are the published series. Turns nobody checked are neither."""
    backfill.run(repo_with_history)
    assert state.ledger(repo_with_history)["turns"] == 0
    record_mod.append(repo_with_history, turn_record(verdict="FAILED"))
    ledger = state.ledger(repo_with_history)
    assert ledger["turns"] == 1 and ledger["caught"] == 1


def test_blame_reads_a_backfilled_turn_without_inventing_evidence(repo_with_history: Path):
    from tycho.views import archaeology

    backfill.run(repo_with_history)
    out = "\n".join(archaeology.blame(repo_with_history, "app.py"))
    assert "UNVERIFIED" in out
    assert "Added `greet()`" in out
    assert "never verified" in out


# --- idempotence -------------------------------------------------------------


def test_running_twice_writes_nothing_the_second_time(repo_with_history: Path):
    first = backfill.run(repo_with_history)
    second = backfill.run(repo_with_history)
    assert second["turns"] == 0
    assert second["skipped"] == first["turns"]
    assert len(rows(repo_with_history)) == first["turns"]


def test_a_turn_already_verified_live_is_not_duplicated(repo_with_history: Path):
    """The live record and the replay derive `ended_at` from different clocks, so the turn id
    differs. Dedup keys on the start boundary, which both derive identically."""
    live = backfill._turns_of(FIXTURE, harness_mod.CLAUDE, repo_with_history)[0]
    record_mod.append(repo_with_history, {**live, "verdict": "VERIFIED", "backfilled": False,
                                          "ended_at": live["ended_at"] + 30})
    result = backfill.run(repo_with_history)
    assert result["turns"] == 1 and result["skipped"] == 1
    assert [r["verdict"] for r in rows(repo_with_history)] == ["VERIFIED", "UNVERIFIED"]


def test_limit_keeps_the_newest_turns(repo_with_history: Path):
    result = backfill.run(repo_with_history, limit=1)
    assert result["turns"] == 1 and result["skipped"] == 1
    only = rows(repo_with_history)[0]
    assert only["files"] == [], "the newest turn is the read-back one"


# --- nothing to do -----------------------------------------------------------


def test_a_repo_with_no_transcripts_reports_nothing_rather_than_failing(git_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", str(tmp_path / "empty"))
    state.dir_for(git_repo).mkdir(parents=True, exist_ok=True)
    assert backfill.run(git_repo) == {"turns": 0, "sessions": 0, "skipped": 0}
    assert backfill.summary({"turns": 0, "sessions": 0, "skipped": 0})[0].endswith(
        "nothing to backfill."
    )


def test_an_unreadable_transcript_contributes_nothing(repo_with_history: Path):
    junk = harness_mod.CLAUDE.history(repo_with_history)[0].parent / "broken.jsonl"
    junk.write_bytes(b"\x00not json at all\n")
    result = backfill.run(repo_with_history)
    assert result["turns"] == 2, "the good session still lands"


def test_available_counts_without_writing(repo_with_history: Path):
    assert backfill.available(repo_with_history) == 2
    assert rows(repo_with_history) == []


# --- the init moment ---------------------------------------------------------


def test_init_seeds_the_record_and_says_what_the_rows_are(repo_with_history: Path):
    from tycho.wire import install

    lines = install._backfill(repo_with_history)
    assert len(rows(repo_with_history)) == 2
    assert any("backfilled 2 turns" in line for line in lines)
    assert any("UNVERIFIED" in line for line in lines), "must not read as a clean bill of health"


def test_init_leaves_an_existing_record_alone(repo_with_history: Path):
    from tycho.wire import install

    record_mod.append(repo_with_history, turn_record())
    assert install._backfill(repo_with_history) == []
    assert len(rows(repo_with_history)) == 1


# --- the record file stays readable -----------------------------------------


def test_rows_are_one_json_object_per_line(repo_with_history: Path):
    backfill.run(repo_with_history)
    text = record_mod.path_for(repo_with_history).read_text(encoding="utf-8")
    assert all(json.loads(line)["schema"] == record_mod.SCHEMA
               for line in text.splitlines() if line.strip())

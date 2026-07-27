"""The CLI wiring for the record-backed commands, and the exit codes it maps.

The commands themselves are tested in `test_digest.py` / `test_archaeology.py` /
`test_review.py` / `test_attest.py`. What is tested *here* is the seam those tests can't
reach: that `cli.main` routes each flag to the right function, and — the only real logic in
cli.py — that `--exit-code` and `--verify` turn a result into the right process exit.

Exit codes are a public contract (see `ExitCode`), so a silent renumbering or a gate that
fires on the wrong finding is a breaking change that no module-level test would catch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tycho import review as review_mod
from tycho.cli import ExitCode, main


def _repo(tmp_path: Path) -> Path:
    """A real git repo with one commit — the baseline `review`/`attest` diff against."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    (tmp_path / "src.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    return tmp_path


def _record(repo: Path, **over) -> dict:
    """One turn record on disk, in the shape the Stop hook writes."""
    rec = {
        "schema": 1, "id": "a" * 16, "session": "s", "harness": "claude",
        "model": "claude-opus-5", "agent_version": "2.1.220",
        "started_at": 1000.0, "ended_at": 2000.0,
        "verdict": "VERIFIED", "stage": "artifact_changed",
        "checks": [], "files": [{"path": "src.py", "kind": "edit", "ts": 1500.0}],
        "commands": [], "claims": ["did a thing"],
    }
    rec.update(over)
    d = repo / ".tycho"
    d.mkdir(exist_ok=True)
    with (d / "turns.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    monkeypatch.chdir(r)
    return r


# --- review --exit-code -------------------------------------------------------


def test_review_is_advisory_by_default_even_with_an_unexercised_change(repo):
    """The default must stay 0 whatever it found — §6 demotes gating deliberately."""
    _record(repo)
    (repo / "src.py").write_text("x = 2\n")
    assert main(["review"]) == ExitCode.OK


def test_review_exit_code_gates_on_an_unexercised_change(repo):
    _record(repo)
    (repo / "src.py").write_text("x = 2\n")  # edited after the turn, nothing run since
    assert main(["review", "--exit-code"]) == ExitCode.UNEXERCISED


def test_review_exit_code_does_not_gate_on_unrecorded_work(repo):
    """A file no recorded turn touched is UNRECORDED, not UNEXERCISED.

    This is the regression that matters: gating on UNRECORDED would fail every honest
    hand-written commit, and on a repo where Tycho was installed yesterday it would fail
    literally everything.
    """
    (repo / "handwritten.py").write_text("y = 1\n")  # no record mentions it
    assert main(["review", "--exit-code"]) == ExitCode.OK


def test_review_exit_code_is_ok_on_a_clean_tree(repo):
    _record(repo)
    assert main(["review", "--exit-code"]) == ExitCode.OK


def test_review_exit_code_is_ok_outside_a_git_repo(tmp_path, monkeypatch):
    """Can't-say must never render as a gate failure."""
    monkeypatch.chdir(tmp_path)
    assert main(["review", "--exit-code"]) == ExitCode.OK


def test_inspect_returns_the_same_lines_review_prints(repo):
    """`review` delegates to `inspect`; if they drift, the gate judges different findings
    than the human reads."""
    _record(repo)
    (repo / "src.py").write_text("x = 2\n")
    lines, findings = review_mod.inspect(repo)
    assert lines == review_mod.review(repo)
    assert findings  # and the findings really are populated on this path


# --- attest --verify ----------------------------------------------------------


def test_attest_verify_is_ok_when_a_commit_has_no_trailer(repo, capsys):
    """No trailer is 'cannot tell', which is exit 0 — not a mismatch."""
    assert main(["attest", "--verify", "HEAD"]) == ExitCode.OK
    assert "no Tycho attestation" in capsys.readouterr().out


def test_attest_verify_reports_mismatch_on_a_forged_trailer(repo, capsys):
    _record(repo)
    (repo / "src.py").write_text("x = 3\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm",
         "forged\n\nTycho-Attestation: sha256:" + "d" * 64],
        check=True,
    )
    assert main(["attest", "--verify", "HEAD"]) == ExitCode.MISMATCH
    assert "does NOT match" in capsys.readouterr().out


def test_attest_bare_prints_nothing_to_attest_when_the_record_is_empty(repo, capsys):
    assert main(["attest"]) == ExitCode.OK
    assert "nothing to attest" in capsys.readouterr().out


# --- log filters --------------------------------------------------------------


def test_log_verdict_filter_reaches_archaeology(repo, capsys):
    _record(repo, id="b" * 16, verdict="FAILED", claims=["the failing one"])
    _record(repo, id="c" * 16, verdict="VERIFIED", claims=["the passing one"])
    assert main(["log", "--verdict", "FAILED"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "the failing one" in out
    assert "the passing one" not in out


def test_log_since_filter_reaches_archaeology(repo, capsys):
    _record(repo, id="d" * 16, ended_at=0.0, claims=["ancient history"])
    assert main(["log", "--since", "2099-01-01"]) == ExitCode.OK
    assert "ancient history" not in capsys.readouterr().out


def test_log_without_filters_still_works(repo, capsys):
    _record(repo, claims=["unfiltered"])
    assert main(["log"]) == ExitCode.OK
    assert "unfiltered" in capsys.readouterr().out


# --- exit-code contract -------------------------------------------------------


def test_exit_codes_are_not_renumbered():
    """CI gates depend on these; a renumbering is a breaking change."""
    assert (ExitCode.OK, ExitCode.FAILED, ExitCode.USAGE, ExitCode.STALE) == (0, 1, 2, 3)
    assert (ExitCode.INTERNAL, ExitCode.UNHEALTHY) == (4, 5)
    assert (ExitCode.UNEXERCISED, ExitCode.MISMATCH) == (6, 7)


def test_review_gate_never_reuses_a_verify_verdict_code():
    """A coverage claim must not be mistakable for a proof that the code is wrong."""
    assert ExitCode.UNEXERCISED not in (ExitCode.FAILED, ExitCode.STALE)

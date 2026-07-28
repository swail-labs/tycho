"""The CLI wiring for the record-backed commands, and the exit codes it maps.

The commands themselves are tested in `test_digest.py` / `test_archaeology.py` /
`test_review.py` / `test_attest.py`. What is tested *here* is the seam those tests can't
reach: that `cli.main` routes each flag to the right function, and — the only real logic in
cli.py — that `--exit-code` and `--verify` turn a result into the right process exit.

Exit codes are a public contract (see `ExitCode`), so a silent renumbering or a gate that
fires on the wrong finding is a breaking change that no module-level test would catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, turn_record

from tycho import record as record_mod
from tycho import review as review_mod
from tycho.cli import ExitCode, main


def _record(repo: Path, **over) -> dict:
    """One turn record on disk, in the shape the Stop hook writes."""
    rec = turn_record(**{
        "id": "a" * 16, "started_at": 1000.0, "ended_at": 2000.0, "stage": "artifact_changed",
        "files": [{"path": "src.py", "kind": "edit", "ts": 1500.0}], "claims": ["did a thing"],
        **over,
    })
    record_mod.append(repo, rec)
    return rec


@pytest.fixture()
def repo(git_repo: Path, monkeypatch) -> Path:
    (git_repo / "src.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "src")
    monkeypatch.chdir(git_repo)
    return git_repo


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


def test_review_exit_code_is_ok_with_nothing_to_judge(repo, tmp_path, monkeypatch):
    """A clean tree and a can't-say must never render as a gate failure."""
    _record(repo)
    assert main(["review", "--exit-code"]) == ExitCode.OK
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


def test_attest_verify_maps_the_three_valued_answer_onto_exit_codes(repo):
    """Cannot-tell is exit 0, not a mismatch. The wording is `test_attest.py`'s job."""
    assert main(["attest", "--verify", "HEAD"]) == ExitCode.OK

    _record(repo)
    (repo / "src.py").write_text("x = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "forged\n\nTycho-Attestation: sha256:" + "d" * 64)
    assert main(["attest", "--verify", "HEAD"]) == ExitCode.MISMATCH


def test_attest_bare_prints_nothing_to_attest_when_the_record_is_empty(repo, capsys):
    assert main(["attest"]) == ExitCode.OK
    assert "nothing to attest" in capsys.readouterr().out


# --- log filters --------------------------------------------------------------


def test_log_filters_reach_archaeology(repo, capsys):
    """What the filters *mean* is `test_archaeology.py`'s; that the flags arrive is this."""
    _record(repo, id="b" * 16, verdict="FAILED", claims=["the failing one"])
    _record(repo, id="c" * 16, verdict="VERIFIED", claims=["the passing one"])
    _record(repo, id="d" * 16, ended_at=0.0, claims=["ancient history"])

    assert main(["log"]) == ExitCode.OK
    assert "the passing one" in capsys.readouterr().out

    assert main(["log", "--verdict", "FAILED"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "the failing one" in out and "the passing one" not in out

    assert main(["log", "--since", "2099-01-01"]) == ExitCode.OK
    assert "ancient history" not in capsys.readouterr().out


# --- argument hygiene ---------------------------------------------------------


def test_show_survives_a_record_with_no_id(repo, capsys):
    """A record whose `id` is null (a truncated write, a hand-edited line) crashed the prefix
    match with AttributeError. `show` reads the record; it must not be broken by it."""
    _record(repo, id=None)
    assert main(["show", "abc"]) == ExitCode.OK
    assert "no turn recorded" in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["0", "-5"])
def test_blame_and_log_refuse_a_non_positive_limit(repo, limit):
    """`-n 0` slices to nothing and renders a touched file as untouched — a wrong answer, not
    an empty one. Argparse refuses it instead."""
    for cmd in (["blame", "src.py", "-n", limit], ["log", "-n", limit]):
        with pytest.raises(SystemExit) as exc:
            main(cmd)
        assert exc.value.code == ExitCode.USAGE


# --- exit-code contract -------------------------------------------------------


def test_exit_codes_are_not_renumbered():
    """CI gates depend on these; a renumbering is a breaking change."""
    assert (ExitCode.OK, ExitCode.FAILED, ExitCode.USAGE, ExitCode.STALE) == (0, 1, 2, 3)
    assert (ExitCode.INTERNAL, ExitCode.UNHEALTHY) == (4, 5)
    assert (ExitCode.UNEXERCISED, ExitCode.MISMATCH) == (6, 7)


def test_review_gate_never_reuses_a_verify_verdict_code():
    """A coverage claim must not be mistakable for a proof that the code is wrong."""
    assert ExitCode.UNEXERCISED not in (ExitCode.FAILED, ExitCode.STALE)

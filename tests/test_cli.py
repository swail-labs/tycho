"""CLI: versioning, `--version`, stable exit codes, `--claim`."""

import json
import re
from pathlib import Path

import pytest

import tycho
from tycho import cli
from tycho.cli import ExitCode
from tycho.model import CheckResult, CheckStatus, Verdict
from tycho.views.report import render

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "transcript_sample.jsonl"


# --- versioning --------------------------------------------------------------

def test_version_is_authoritative_and_not_placeholder():
    # pyproject reads this via [tool.hatch.version]; it's the single source.
    assert tycho.__version__ != "0.0.0"
    # semver-ish x.y.z with an optional prerelease suffix (e.g. 0.1.0-rc.1).
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", tycho.__version__)


def test_version_flag_prints_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:  # argparse's `version` action exits
        cli.main(["--version"])
    assert exc.value.code == 0
    assert tycho.__version__ in capsys.readouterr().out


def test_help_flag_exits_zero(capsys):
    # argparse gives `--help` for free at both levels; assert it stays wired.
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "verify" in capsys.readouterr().out


# --- stable exit codes -------------------------------------------------------

def test_exit_codes_are_pinned():
    # These are a public contract — a CI gate depends on them. Renumbering breaks users.
    assert (ExitCode.OK, ExitCode.FAILED, ExitCode.USAGE, ExitCode.STALE, ExitCode.INTERNAL) == (
        0, 1, 2, 3, 4,
    )


def test_usage_error_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense-command"])
    assert exc.value.code == ExitCode.USAGE


def test_run_forwards_child_exit_code():
    # `tycho run` must forward the child's real exit code unchanged — that is the whole
    # point: an un-maskable status for command_execution to trust.
    import sys

    assert cli.main(["run", "--", sys.executable, "-c", "import sys; sys.exit(7)"]) == 7
    assert cli.main(["run", "--", sys.executable, "-c", "import sys; sys.exit(0)"]) == 0


def test_run_without_command_is_usage_error(capsys):
    assert cli.main(["run"]) == ExitCode.USAGE


def test_verify_exits_internal_on_unreadable_transcript(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    code = cli.main(["verify", "--session", str(missing)])
    assert code == ExitCode.INTERNAL
    err = capsys.readouterr().err
    assert "could not verify" in err and "Traceback" not in err


def test_verify_exits_internal_on_malformed_config(tmp_path, monkeypatch, capsys):
    (tmp_path / ".tycho.toml").write_text("this is not [valid toml")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"content": []}}))
    monkeypatch.chdir(tmp_path)
    code = cli.main(["verify", "--session", str(transcript)])
    assert code == ExitCode.INTERNAL
    assert "could not verify" in capsys.readouterr().err


def test_verify_ok_when_no_session_discovered(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.harness_mod, "discover", lambda cwd, only=None: (None, None))
    assert cli.main(["verify"]) == ExitCode.OK
    assert "no recent session found" in capsys.readouterr().out


# --- --claim is wired into the report ---------------------------------------

def test_claim_is_echoed_in_report():
    out = render(Verdict.VERIFIED, [CheckResult("c", CheckStatus.PASS, "e")], claim="added rate limiting")
    assert 'claim: "added rate limiting"' in out
    assert "Tycho: VERIFIED" in out


def test_report_without_claim_is_unchanged():
    results = [CheckResult("c", CheckStatus.PASS, "e")]
    assert render(Verdict.VERIFIED, results) == render(Verdict.VERIFIED, results, claim=None)


def test_verify_echoes_claim_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["verify", "--session", str(CLAUDE_FIXTURE), "--claim", "fixed the thing"])
    assert 'claim: "fixed the thing"' in capsys.readouterr().out

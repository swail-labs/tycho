"""M1 walking-skeleton checks: the verdict reduction, render, and config default."""

from pathlib import Path

from tycho import verify as engine
from tycho.config import Config, load
from tycho.model import CheckResult, CheckStatus, Verdict
from tycho.report import render


def _r(status: CheckStatus) -> CheckResult:
    return CheckResult("c", status, "e")


def test_fail_beats_everything():
    results = [_r(CheckStatus.PASS), _r(CheckStatus.STALE), _r(CheckStatus.FAIL)]
    assert engine.verdict_of(results) == Verdict.FAILED


def test_stale_beats_pass():
    assert engine.verdict_of([_r(CheckStatus.PASS), _r(CheckStatus.STALE)]) == Verdict.STALE


def test_one_pass_verifies_despite_indeterminate():
    results = [_r(CheckStatus.PASS), _r(CheckStatus.INDETERMINATE), _r(CheckStatus.UNSUPPORTED)]
    assert engine.verdict_of(results) == Verdict.VERIFIED


def test_all_unsupported_is_unsupported():
    assert engine.verdict_of([_r(CheckStatus.UNSUPPORTED)]) == Verdict.UNSUPPORTED


def test_file_and_git_state_alone_do_not_verify():
    # "the edited files exist and are in git" is true of any session that touched
    # a file — and stays true turns later once the work is committed. Corroborating
    # evidence can't carry a VERIFIED on its own.
    results = [
        CheckResult("file_state", CheckStatus.PASS, "e"),
        CheckResult("git_state", CheckStatus.PASS, "e"),
        CheckResult("command_execution", CheckStatus.UNSUPPORTED, "e"),
    ]
    assert engine.verdict_of(results) == Verdict.INDETERMINATE


def test_substantive_pass_verifies_alongside_weak_ones():
    results = [
        CheckResult("file_state", CheckStatus.PASS, "e"),
        CheckResult("command_execution", CheckStatus.PASS, "e"),
    ]
    assert engine.verdict_of(results) == Verdict.VERIFIED


def test_only_indeterminate_is_indeterminate():
    assert engine.verdict_of([_r(CheckStatus.INDETERMINATE)]) == Verdict.INDETERMINATE


def test_empty_is_indeterminate():
    assert engine.verdict_of([]) == Verdict.INDETERMINATE


def test_render_has_header_and_a_line_per_check():
    results = [
        _r(CheckStatus.STALE),
        CheckResult("test_freshness", CheckStatus.PASS, "e"),
    ]
    out = render(engine.verdict_of(results), results)
    assert out.startswith("🔍 Tycho: STALE")
    assert out.count("\n") == len(results)  # header + one line each
    assert "test_freshness" in out


def test_config_default_when_missing(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg == Config()
    assert cfg.scope_include == ()


def test_config_reads_scope_and_disables(tmp_path: Path):
    (tmp_path / ".tycho.toml").write_text(
        '[scope]\ninclude = ["src/**"]\n[checks]\ndisable = ["scope_drift"]\n'
    )
    cfg = load(tmp_path)
    assert cfg.scope_include == ("src/**",)
    assert cfg.disabled_checks == ("scope_drift",)

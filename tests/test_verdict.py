"""The verdict reduction, render, and config default."""

from pathlib import Path

from tycho import verify as engine
from tycho.config import Config, load
from tycho.model import CheckResult, CheckStatus, Verdict
from tycho.report import render


def _r(status: CheckStatus, name: str = "command_execution") -> CheckResult:
    """Named for a substantive check by default — only those can carry a VERIFIED, so a
    placeholder name would exercise the fallback rather than the rule under test."""
    return CheckResult(name, status, "e")


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


def test_no_absence_of_a_problem_can_carry_a_verified():
    """The turn that motivated the allowlist: an agent edits a file, runs no tests, and says
    "all tests pass". Every check that can pass here passes on the absence of a problem — the
    file is on disk, git agrees, the edit was inside the scope, no assertion was neutralized,
    no skip was injected. None of them looked at a test run, because there wasn't one.

    Under the old denylist `scope_drift` was simply never added to it, so setting a scope —
    which `tycho init` prompts for — turned this exact turn from INDETERMINATE into a green.
    """
    results = [
        CheckResult("file_state", CheckStatus.PASS, "e"),
        CheckResult("git_state", CheckStatus.PASS, "e"),
        CheckResult("scope_drift", CheckStatus.PASS, "e"),
        CheckResult("assertion_weakening", CheckStatus.PASS, "e"),
        CheckResult("skip_mock_injection", CheckStatus.PASS, "e"),
        CheckResult("tool_call_provenance", CheckStatus.PASS, "e"),
        CheckResult("command_execution", CheckStatus.UNSUPPORTED, "e"),
        CheckResult("test_freshness", CheckStatus.UNSUPPORTED, "e"),
    ]
    assert engine.verdict_of(results) == Verdict.INDETERMINATE


def test_a_new_check_cannot_mint_a_green_by_default():
    """The allowlist's whole point: an unrecognized check name is not evidence. Whoever adds a
    check that genuinely proves a claim must add it to `_SUBSTANTIVE_CHECKS` deliberately."""
    results = [
        CheckResult("some_future_check", CheckStatus.PASS, "e"),
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

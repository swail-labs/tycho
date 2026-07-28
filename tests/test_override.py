""" — the agent verdict override (OVERRIDDEN): a per-check, logged, opt-in escape
hatch that breaks an unclearable relay loop without ever masquerading as VERIFIED."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tycho import cli, config, hook, init as init_mod, report, state, status
from tycho.model import CheckResult, CheckStatus, Verdict
from tycho.model import Verdict as V


def test_overridden_is_a_distinct_verdict_value():
    assert Verdict.OVERRIDDEN == "OVERRIDDEN"
    assert Verdict.OVERRIDDEN != Verdict.VERIFIED


def test_render_labels_overridden_as_not_proven():
    results = [CheckResult("test_freshness", CheckStatus.STALE, "edited after last passing test")]
    out = report.render(Verdict.OVERRIDDEN, results)
    assert "OVERRIDDEN" in out
    assert "not proven" in out.lower()
    # the underlying adverse check is still shown verbatim
    assert "test_freshness" in out


def test_status_paints_overridden_its_own_shade_not_green():
    assert "OVERRIDDEN" in status._VERDICT_COLOUR
    assert status._VERDICT_COLOUR["OVERRIDDEN"] != status._VERDICT_COLOUR["VERIFIED"]


def test_override_flag_defaults_off(tmp_path: Path):
    assert config.load(tmp_path).override_enabled is False


def test_override_flag_round_trips_in_tycho_toml(tmp_path: Path):
    config.set_override(tmp_path, True)
    text = config.path(tmp_path).read_text(encoding="utf-8")
    assert "[override]" in text and "enabled = true" in text
    assert config.load(tmp_path).override_enabled is True
    config.set_override(tmp_path, False)
    assert "enabled = false" in config.path(tmp_path).read_text(encoding="utf-8")


def test_set_override_preserves_scope_and_relay(tmp_path: Path):
    config.set_scope(tmp_path, ["src/**"])
    config.set_relay(tmp_path, True)
    config.set_override(tmp_path, True)
    cfg = config.load(tmp_path)
    assert cfg.override_enabled is True
    assert cfg.relay_enabled is True
    assert cfg.scope_include == ("src/**",)


def test_override_capability_defaults_off_and_toggles(tmp_path: Path):
    assert state.override_enabled(tmp_path) is False
    state.set_override_enabled(tmp_path, True)
    assert state.override_enabled(tmp_path) is True


def test_record_override_populates_marker_and_log(tmp_path: Path):
    state.record_override(tmp_path, "test_freshness", "CI YAML; verified by CI 5/5")
    marks = state.overrides(tmp_path)
    assert marks == [{"check": "test_freshness", "reason": "CI YAML; verified by CI 5/5"}]
    log = state.override_log(tmp_path)
    assert len(log) == 1
    assert log[0]["check"] == "test_freshness"
    assert log[0]["reason"] == "CI YAML; verified by CI 5/5"
    assert isinstance(log[0]["at"], (int, float))


def test_overrides_accumulate_and_clear(tmp_path: Path):
    state.record_override(tmp_path, "test_freshness", "a")
    state.record_override(tmp_path, "scope_drift", "b")
    assert {m["check"] for m in state.overrides(tmp_path)} == {"test_freshness", "scope_drift"}
    state.clear_overrides(tmp_path)
    assert state.overrides(tmp_path) == []
    # the audit log survives a marker clear (it is the permanent record)
    assert len(state.override_log(tmp_path)) == 2


def test_overrides_empty_when_unreadable(tmp_path: Path):
    assert state.overrides(tmp_path) == []


def _results(**status_by_name):
    return [CheckResult(n, s, f"{n} evidence") for n, s in status_by_name.items()]


def test_apply_overrides_greens_a_cleared_turn(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "test_freshness", "CI YAML; not test-verifiable")
    results = _results(git_provenance=CheckStatus.PASS, test_freshness=CheckStatus.STALE)
    assert hook._apply_overrides(tmp_path, results, V.STALE) is V.OVERRIDDEN


def test_apply_overrides_keeps_a_surviving_failure(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "test_freshness", "reason")
    results = _results(command_execution=CheckStatus.FAIL, test_freshness=CheckStatus.STALE)
    # a real FAIL survives the override → verdict stays FAILED, never OVERRIDDEN
    assert hook._apply_overrides(tmp_path, results, V.FAILED) is V.FAILED


def test_apply_overrides_noop_on_passing_check(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "git_provenance", "reason")  # names a PASS check
    results = _results(git_provenance=CheckStatus.PASS, test_freshness=CheckStatus.STALE)
    assert hook._apply_overrides(tmp_path, results, V.STALE) is V.STALE


def test_apply_overrides_ignored_when_capability_off(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)  # relay on, but capability off → real verdict stands
    state.record_override(tmp_path, "test_freshness", "reason")  # recorded but capability OFF
    results = _results(git_provenance=CheckStatus.PASS, test_freshness=CheckStatus.STALE)
    assert hook._apply_overrides(tmp_path, results, V.STALE) is V.STALE


def test_apply_overrides_ignored_when_relay_off(tmp_path: Path):
    # The override breaks a relay loop; with the relay off there is nothing to break, so the
    # real verdict stands even though the capability is on and a check is disputed.
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "test_freshness", "reason")
    results = _results(git_provenance=CheckStatus.PASS, test_freshness=CheckStatus.STALE)
    assert hook._apply_overrides(tmp_path, results, V.STALE) is V.STALE


def test_relay_treats_overridden_as_terminal(tmp_path: Path):
    from types import SimpleNamespace
    state.set_relay_enabled(tmp_path, True)
    state.bump_relay_streak(tmp_path)
    out = hook._relay_output(tmp_path, SimpleNamespace(name="claude"),
                             SimpleNamespace(name="OVERRIDDEN"), "report", "adverse")
    assert out is None
    assert state.relay_streak(tmp_path) == 0


def test_relay_guard_advertises_override_only_when_enabled(tmp_path: Path):
    off = hook._relay_guard(1, 3, override_on=False)
    on = hook._relay_guard(1, 3, override_on=True)
    assert "tycho override" not in off
    assert "tycho override" in on


def _submit_prompt(repo: Path) -> None:
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"cwd": str(repo)}))
    try:
        hook.prompt_submit()
    finally:
        sys.stdin = saved


def test_new_user_prompt_clears_the_override_marker(tmp_path: Path):
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "test_freshness", "reason")
    assert state.overrides(tmp_path) != []
    _submit_prompt(tmp_path)
    assert state.overrides(tmp_path) == []          # marker gone — no leak into the next turn
    assert len(state.override_log(tmp_path)) == 1    # but the audit log persists


def test_cli_override_reports_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["override"]) == 0
    assert "OFF" in capsys.readouterr().out


def test_cli_override_on_then_off(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["override", "--on"]) == 0
    assert "ON" in capsys.readouterr().out
    assert state.override_enabled(tmp_path) is True
    assert cli.main(["override", "--off"]) == 0
    assert state.override_enabled(tmp_path) is False


def test_cli_override_action_records_when_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    capsys.readouterr()
    assert cli.main(["override", "test_freshness", "CI YAML; verified by CI"]) == 0
    out = capsys.readouterr().out
    assert "test_freshness" in out
    assert state.overrides(tmp_path) == [
        {"check": "test_freshness", "reason": "CI YAML; verified by CI"}
    ]


def test_cli_override_action_refuses_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["override", "test_freshness", "reason"]) == 0  # never a hard error
    out = capsys.readouterr().out
    assert "off" in out.lower() and "--on" in out
    assert state.overrides(tmp_path) == []  # nothing recorded


def test_cli_override_action_rejects_empty_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    capsys.readouterr()
    assert cli.main(["override", "test_freshness", "   "]) == 0
    assert "reason" in capsys.readouterr().out.lower()
    assert state.overrides(tmp_path) == []


def test_cli_override_on_and_off_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["override", "--on", "--off"])


def test_init_installs_override_slash_commands(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    commands = tmp_path / ".claude" / "commands"
    for name in ("tycho-override.md", "tycho-override-on.md", "tycho-override-off.md"):
        assert (commands / name).exists()
    assert "override --on" in (commands / "tycho-override-on.md").read_text(encoding="utf-8")


def test_veto_removes_override_and_blocks_reapply(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "test_freshness", "reason")
    state.veto_override(tmp_path, "test_freshness")
    # marker no longer carries it, and it is now vetoed
    assert state.overrides(tmp_path) == []
    assert state.vetoed(tmp_path) == ["test_freshness"]
    # even if the override is recorded again, _apply_overrides must not green the turn
    state.record_override(tmp_path, "test_freshness", "reason again")
    results = _results(git_provenance=CheckStatus.PASS, test_freshness=CheckStatus.STALE)
    assert hook._apply_overrides(tmp_path, results, V.STALE) is V.STALE


def test_unveto_lifts_the_block(tmp_path: Path):
    state.set_override_enabled(tmp_path, True)
    state.veto_override(tmp_path, "test_freshness")
    state.unveto_override(tmp_path, "test_freshness")
    assert state.vetoed(tmp_path) == []


def test_veto_persists_across_a_user_prompt(tmp_path: Path):
    state.set_override_enabled(tmp_path, True)
    state.veto_override(tmp_path, "test_freshness")
    _submit_prompt(tmp_path)                       # a fresh turn clears the marker...
    assert state.vetoed(tmp_path) == ["test_freshness"]   # ...but NOT the veto (it's cross-turn)


def test_veto_is_logged(tmp_path: Path):
    state.set_override_enabled(tmp_path, True)
    state.veto_override(tmp_path, "test_freshness")
    log = state.override_log(tmp_path)
    assert any(e.get("vetoed") and e.get("check") == "test_freshness" for e in log)


def test_cli_veto_removes_override_and_lists_in_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    cli.main(["override", "test_freshness", "reason"])
    capsys.readouterr()
    assert cli.main(["override", "--veto", "test_freshness"]) == 0
    assert "test_freshness" in capsys.readouterr().out
    assert state.overrides(tmp_path) == []
    assert state.vetoed(tmp_path) == ["test_freshness"]


def test_cli_bare_veto_vetoes_all_active(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    cli.main(["override", "test_freshness", "a"])
    cli.main(["override", "scope_drift", "b"])
    capsys.readouterr()
    assert cli.main(["override", "--veto"]) == 0
    assert set(state.vetoed(tmp_path)) == {"test_freshness", "scope_drift"}


def test_cli_record_refuses_a_vetoed_check(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    cli.main(["override", "--veto", "test_freshness"])
    capsys.readouterr()
    assert cli.main(["override", "test_freshness", "trying again"]) == 0
    out = capsys.readouterr().out.lower()
    assert "veto" in out
    assert state.overrides(tmp_path) == []   # refused


def test_cli_unveto(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    cli.main(["override", "--veto", "test_freshness"])
    capsys.readouterr()
    assert cli.main(["override", "--unveto", "test_freshness"]) == 0
    assert state.vetoed(tmp_path) == []


def test_apply_overrides_never_downgrades_a_real_verified(tmp_path: Path):
    # overriding a non-adverse check on an already-VERIFIED turn must leave VERIFIED —
    # OVERRIDDEN is a downgrade (proven → not-proven) and must never replace a real green verdict.
    state.set_relay_enabled(tmp_path, True)
    state.set_override_enabled(tmp_path, True)
    state.record_override(tmp_path, "scope_drift", "believed n/a")  # a non-adverse (UNSUPPORTED) check
    results = _results(command_execution=CheckStatus.PASS, scope_drift=CheckStatus.UNSUPPORTED)
    assert hook._apply_overrides(tmp_path, results, V.VERIFIED) is V.VERIFIED


def test_cli_override_rejects_empty_check_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    capsys.readouterr()
    assert cli.main(["override", "   ", "reason"]) == 0     # never a hard error
    out = capsys.readouterr().out.lower()
    assert "check" in out
    assert state.overrides(tmp_path) == []                  # nothing recorded
    assert state.override_log(tmp_path) == []               # and nothing in the permanent audit log


def test_cli_override_rejects_unknown_check_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    capsys.readouterr()
    assert cli.main(["override", "definitely_not_a_check", "reason"]) == 0
    out = capsys.readouterr().out.lower()
    assert "unknown check" in out
    assert state.overrides(tmp_path) == []
    assert state.override_log(tmp_path) == []


def test_cli_override_still_records_a_known_check(tmp_path, monkeypatch, capsys):
    # regression guard: the new name checks must not block a legitimate override
    monkeypatch.chdir(tmp_path)
    cli.main(["override", "--on"])
    capsys.readouterr()
    assert cli.main(["override", "test_freshness", "CI-verified"]) == 0
    assert state.overrides(tmp_path) == [{"check": "test_freshness", "reason": "CI-verified"}]


def test_overridden_verdict_tells_user_how_to_veto_or_disable(tmp_path, monkeypatch):
    # relay + override ON, agent overrode the only adverse check -> verdict OVERRIDDEN.
    # The Stop output the HUMAN sees must explain the veto/disable escape hatches.
    from tycho import hook, state
    from tycho.model import Verdict

    # Arrange state: enable relay+override, record an override on "test_freshness".
    repo = tmp_path
    state.set_relay_enabled(repo, True)
    state.set_override_enabled(repo, True)
    state.record_override(repo, "test_freshness", "not applicable: docs-only turn")

    class _HumanHarness:  # human-only channel present (like Claude/Codex)
        notice_output = staticmethod(lambda t: {"systemMessage": t})

    results = _results(test_freshness=CheckStatus.STALE)
    out = hook._override_notice(repo, _HumanHarness, Verdict.OVERRIDDEN, results)
    assert "test_freshness" in out
    assert "tycho override --veto" in out
    assert "tycho override --off" in out

    # Non-OVERRIDDEN verdict -> no notice.
    assert hook._override_notice(repo, _HumanHarness, Verdict.VERIFIED, results) == ""

    # Model-facing harness (no human-only channel) -> suppressed.
    class _ModelHarness:
        notice_output = None
    assert hook._override_notice(repo, _ModelHarness, Verdict.OVERRIDDEN, results) == ""

    # An override on a check that actually PASSed is a no-op — it must NOT be named as
    # "set aside" (mirrors _apply_overrides' disputed & non-PASS intersection).
    state.record_override(repo, "git_provenance", "cosmetic — this one passed")
    passed_results = _results(test_freshness=CheckStatus.STALE, git_provenance=CheckStatus.PASS)
    out2 = hook._override_notice(repo, _HumanHarness, Verdict.OVERRIDDEN, passed_results)
    assert "test_freshness" in out2 and "git_provenance" not in out2


_CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def test_run_end_to_end_surfaces_override_notice_in_systemMessage(tmp_path: Path):
    # seam: `_override_notice` is unit-tested above but never driven through
    # `hook.run()` itself, so a dropped `+ override_notice` at the format_output call site
    # would go unnoticed. Reuse test_relay's real-transcript pattern (sample fixture verifies
    # to FAILED on `file_state`) so this exercises the actual run() integration end to end.
    repo = tmp_path
    state.set_relay_enabled(repo, True)
    state.set_override_enabled(repo, True)
    state.record_override(repo, "file_state", "not applicable: docs-only turn")

    payload = json.dumps({"cwd": str(repo), "transcript_path": str(_CLAUDE_FIXTURE.resolve())})
    out = hook.run(payload)

    assert out is not None
    text = out["systemMessage"]
    assert "OVERRIDDEN" in text
    assert "tycho override --veto" in text
    assert "tycho override --off" in text

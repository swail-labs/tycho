""" — the opt-in verdict relay: the agent gets to see its own verdict.

Three things are load-bearing here, and the tests are grouped by them:

1. **Off by default.** No opt-in means the Stop hook emits exactly the human-only output it
   always did — no `additionalContext`, so no agent context is used and no extra generation is
   spent. This is the whole promise: Tycho free never touches the user's context uninvited.
2. **Bounded.** When on, a non-VERIFIED verdict is fed back to Claude as Stop
   `additionalContext`, or Codex as `decision:block` + `reason`, but only up to `relay_max()`
   auto-continuations per user turn.
3. **Scoped to one user turn.** A real user prompt resets the leash; a VERIFIED verdict clears
   it. Auto-continuations don't re-fire UserPromptSubmit, so the count only resets on the human's
   own prompts — exactly what the bound is scoped to.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tycho import cli, config, hook, state
from tycho import init as init_mod

CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "codex_transcript_sample.jsonl"


def _claude_payload(repo: Path) -> str:
    """A Claude Stop payload for the sample transcript — which verifies to FAILED (non-VERIFIED),
    the verdict the relay acts on."""
    return json.dumps({"cwd": str(repo), "transcript_path": str(CLAUDE_FIXTURE.resolve())})


def _codex_payload(repo: Path) -> str:
    return json.dumps({
        "cwd": str(repo),
        "transcript_path": str(CODEX_FIXTURE.resolve()),
        "hook_event_name": "Stop",
        "turn_id": "turn-current",
    })


def _submit_prompt(repo: Path) -> None:
    """Drive the UserPromptSubmit hook as a real user prompt would."""
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"cwd": str(repo)}))
    try:
        hook.prompt_submit()
    finally:
        sys.stdin = saved


# --- state: the toggle and the bounded counter -------------------------------

def test_relay_is_off_by_default(tmp_path: Path):
    assert state.relay_enabled(tmp_path) is False


def test_toggle_on_then_off(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    assert state.relay_enabled(tmp_path) is True
    state.set_relay_enabled(tmp_path, False)
    assert state.relay_enabled(tmp_path) is False


def test_streak_bumps_and_resets(tmp_path: Path):
    assert state.relay_streak(tmp_path) == 0
    assert state.bump_relay_streak(tmp_path) == 1
    assert state.bump_relay_streak(tmp_path) == 2
    assert state.relay_streak(tmp_path) == 2
    state.reset_relay_streak(tmp_path)
    assert state.relay_streak(tmp_path) == 0


def test_toggle_resets_the_streak(tmp_path: Path):
    state.bump_relay_streak(tmp_path)
    state.set_relay_enabled(tmp_path, True)  # a toggle starts a fresh leash
    assert state.relay_streak(tmp_path) == 0


def test_flag_lives_in_tycho_toml_and_round_trips(tmp_path: Path):
    # The on/off setting is the hand-editable `.tycho.toml` [relay] key, not a sentinel.
    state.set_relay_enabled(tmp_path, True)
    text = config.path(tmp_path).read_text(encoding="utf-8")
    assert "[relay]" in text and "enabled = true" in text
    assert config.load(tmp_path).relay_enabled is True
    state.set_relay_enabled(tmp_path, False)
    assert "enabled = false" in config.path(tmp_path).read_text(encoding="utf-8")


def test_toggling_relay_preserves_scope(tmp_path: Path):
    # Writing the relay flag must not wipe an existing scope allowlist (shared render path).
    config.set_scope(tmp_path, ["src/**"])
    state.set_relay_enabled(tmp_path, True)
    cfg = config.load(tmp_path)
    assert cfg.relay_enabled is True and cfg.scope_include == ("src/**",)


def test_relay_max_default_and_override(monkeypatch):
    monkeypatch.delenv("TYCHO_RELAY_MAX", raising=False)
    assert state.relay_max() == 3
    monkeypatch.setenv("TYCHO_RELAY_MAX", "5")
    assert state.relay_max() == 5
    monkeypatch.setenv("TYCHO_RELAY_MAX", "garbage")
    assert state.relay_max() == 3  # junk falls back, never raises inside the hook
    monkeypatch.setenv("TYCHO_RELAY_MAX", "-2")
    assert state.relay_max() == 0  # floored


# --- hook.run: off by default, injects when on -------------------------------

def test_off_by_default_emits_only_systemMessage(tmp_path: Path):
    out = hook.run(_claude_payload(tmp_path))
    assert out is not None
    assert set(out) == {"systemMessage"}  # no additionalContext — no agent context used


def test_on_injects_additionalContext_for_non_verified(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_claude_payload(tmp_path))
    assert out is not None
    assert "Tycho:" in out["systemMessage"]  # the human still sees the verdict
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "Stop"
    assert "Tycho:" in hso["additionalContext"]
    assert "not a new instruction" in hso["additionalContext"]  # the report-not-a-command guard
    assert state.relay_streak(tmp_path) == 1


def test_codex_stop_relays_end_to_end(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_codex_payload(tmp_path))
    assert out["decision"] == "block"
    assert "Tycho:" in out["systemMessage"]
    assert "Tycho:" in out["reason"]
    assert "not a new instruction" in out["reason"]


def test_codex_fabricated_web_search_triggers_relay(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-14T18:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-current"},
        },
        {
            "timestamp": "2026-07-14T18:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I searched the web for the docs."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    state.set_relay_enabled(tmp_path, True)
    payload = json.loads(_codex_payload(tmp_path))
    payload["transcript_path"] = str(transcript)
    out = hook.run(json.dumps(payload))
    assert out["decision"] == "block"
    assert "tool_call_provenance" in out["reason"]
    assert "claimed a web search/fetch with no matching tool call" in out["reason"]


def test_codex_relay_does_not_recheck_the_previous_iteration(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-14T18:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-current"},
        },
        {
            "timestamp": "2026-07-14T18:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I searched the web."}],
            },
        },
        {
            "timestamp": "2026-07-14T18:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        '<hook_prompt hook_run_id="stop:1">[TYCHO] The above is an '
                        "automated verification of the turn you just finished</hook_prompt>"
                    ),
                }],
            },
        },
        {
            "timestamp": "2026-07-14T18:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Deliberate test succeeded."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    state.set_relay_enabled(tmp_path, True)
    payload = json.loads(_codex_payload(tmp_path))
    payload["transcript_path"] = str(transcript)
    assert hook.run(json.dumps(payload)) is None


def test_additionalContext_is_adverse_only_not_the_full_verdict(tmp_path: Path):
    # The harness renders additionalContext verbatim as "Stop hook feedback"; a full copy there
    # reshows the whole verdict the human already reads on systemMessage. The model-facing copy
    # carries only the adverse line(s); the human channel still carries every check.
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_claude_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "✗" in ctx  # the failing check is present, so the agent knows what to fix
    assert "•" not in ctx  # ...but the non-adverse (UNSUPPORTED) lines are not re-listed
    assert "•" in out["systemMessage"]  # the human still sees every check


def test_guard_tells_agent_not_to_regurgitate_the_verdict(tmp_path: Path):
    # The human already sees the verdict on systemMessage; the model-facing guard must tell the
    # agent not to reprint it, so the relay drives a fix rather than an echo of the report.
    state.set_relay_enabled(tmp_path, True)
    ctx = hook.run(_claude_payload(tmp_path))["hookSpecificOutput"]["additionalContext"]
    assert "do not" in ctx.lower() and "re-list" in ctx.lower()
    # The report is still handed over (the agent needs to know which check failed to fix it).
    assert "Tycho:" in ctx


def test_relay_is_bounded_then_stops_hard(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)  # relay_max default 3
    injected = ["hookSpecificOutput" in hook.run(_claude_payload(tmp_path)) for _ in range(6)]
    # Exactly relay_max continuations, then a hard stop that does NOT re-arm mid-turn.
    assert injected == [True, True, True, False, False, False]
    assert state.relay_streak(tmp_path) == 3


def test_a_real_user_prompt_rearms_the_leash(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    for _ in range(3):
        hook.run(_claude_payload(tmp_path))  # spend the leash
    assert "hookSpecificOutput" not in hook.run(_claude_payload(tmp_path))  # spent
    _submit_prompt(tmp_path)  # a real user prompt opens a fresh turn
    assert state.relay_streak(tmp_path) == 0
    assert "hookSpecificOutput" in hook.run(_claude_payload(tmp_path))  # armed again


def test_final_attempt_announces_it_is_the_last(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TYCHO_RELAY_MAX", "1")
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_claude_payload(tmp_path))
    assert "final automatic re-check" in out["hookSpecificOutput"]["additionalContext"]


def test_guard_uses_TYCHO_prefix_and_points_at_the_relay_command(tmp_path: Path):
    # match the [TYCHO] status-line casing, and tell the user how to turn the relay off.
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_claude_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[TYCHO]" in ctx and "[Tycho]" not in ctx
    assert "/tycho-relay" in ctx
    # The human-facing systemMessage also carries the manage/turn-off pointer.
    assert "/tycho-relay" in out["systemMessage"]


# --- _relay_output unit cases the fixture can't reach ------------------------

def _fake(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def test_verified_verdict_never_injects_and_clears_streak(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.bump_relay_streak(tmp_path)
    out = hook._relay_output(tmp_path, _fake("claude"), _fake("VERIFIED"), "report", "adverse")
    assert out is None  # nothing to fix — the turn ends
    assert state.relay_streak(tmp_path) == 0


def test_codex_relays_with_block_reason(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    out = hook._relay_output(
        tmp_path, _fake("codex"), _fake("FAILED"), "full report", "adverse report"
    )
    assert out["decision"] == "block"
    assert "adverse report" in out["reason"]
    assert "not a new instruction" in out["reason"]
    assert "full report" in out["systemMessage"]
    assert state.relay_streak(tmp_path) == 1


def test_other_harnesses_never_inject_even_when_enabled(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    for h in ("cursor", "opencode"):
        assert hook._relay_output(tmp_path, _fake(h), _fake("FAILED"), "report", "adverse") is None


def test_disabled_relay_returns_none(tmp_path: Path):
    assert hook._relay_output(tmp_path, _fake("claude"), _fake("FAILED"), "report", "adverse") is None


# --- cli: `tycho relay [--on|--off]` -----------------------------------------

def test_cli_relay_reports_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["relay"]) == 0
    assert "OFF" in capsys.readouterr().out


def test_cli_relay_on_then_off(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["relay", "--on"]) == 0
    assert "ON" in capsys.readouterr().out
    assert state.relay_enabled(tmp_path) is True
    assert cli.main(["relay", "--off"]) == 0
    assert "OFF" in capsys.readouterr().out
    assert state.relay_enabled(tmp_path) is False


def test_cli_relay_on_and_off_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["relay", "--on", "--off"])


def test_bare_cli_relay_shows_how_to_toggle(tmp_path, monkeypatch, capsys):
    # `/tycho-relay` (bare) is the single status + how-to-toggle surface the fire message points at.
    monkeypatch.chdir(tmp_path)
    cli.main(["relay"])
    out = capsys.readouterr().out
    assert "--on" in out and "--off" in out and "/tycho-relay-on" in out


# --- init: the setup question and the slash commands -------------------------

def _init_claude(repo: Path, **kw) -> list[str]:
    (repo / ".claude").mkdir(exist_ok=True)
    return init_mod.init(repo, only="claude", assume_yes=True, **kw)


def _init_codex(repo: Path, **kw) -> list[str]:
    (repo / ".codex").mkdir(exist_ok=True)
    return init_mod.init(repo, only="codex", assume_yes=True, **kw)


def test_install_leaves_relay_off_by_default(tmp_path: Path):
    _init_claude(tmp_path)  # scripted install: never enables silently
    assert state.relay_enabled(tmp_path) is False


def test_setup_question_yes_enables_relay(tmp_path: Path):
    lines = _init_claude(tmp_path, relay_confirm=lambda: True)
    assert state.relay_enabled(tmp_path) is True
    assert any("verdict relay ON" in line for line in lines)


def test_codex_setup_question_yes_enables_relay(tmp_path: Path):
    lines = _init_codex(tmp_path, relay_confirm=lambda: True)
    assert state.relay_enabled(tmp_path) is True
    assert any("verdict relay ON" in line for line in lines)


def test_setup_question_no_leaves_it_off(tmp_path: Path):
    _init_claude(tmp_path, relay_confirm=lambda: False)
    assert state.relay_enabled(tmp_path) is False


def test_setup_reflects_choice_in_tycho_toml(tmp_path: Path):
    # The setup choice becomes the initial `.tycho.toml` [relay] value.
    _init_claude(tmp_path, relay_confirm=lambda: True)
    assert config.load(tmp_path).relay_enabled is True


def test_prompt_skipped_when_config_already_present(tmp_path: Path):
    # User brought their own .tycho.toml before init → don't re-ask or override; just say how to change.
    config.ensure(tmp_path)  # a pre-existing config (relay off)
    lines = _init_claude(tmp_path, relay_confirm=lambda: True)  # would enable *if* it asked
    assert state.relay_enabled(tmp_path) is False  # respected the existing file, not the prompt
    assert any("tycho relay --on|--off" in line and config.CONFIG_NAME in line for line in lines)


def test_slash_commands_include_relay(tmp_path: Path):
    _init_claude(tmp_path)
    commands = tmp_path / ".claude" / "commands"
    for name in ("tycho-relay.md", "tycho-relay-on.md", "tycho-relay-off.md"):
        assert (commands / name).exists()
    assert "relay --on" in (commands / "tycho-relay-on.md").read_text()

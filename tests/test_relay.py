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

from tycho import cli
from tycho.store import config, state
from tycho.wire import hook
from tycho.wire import install as init_mod

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
    assert "Tycho:" in out["reason"]
    assert "not a new instruction" in out["reason"]
    # No `systemMessage`: Codex accepts the field and renders it nowhere (probed against
    # 0.146.0, CLI and desktop app), so `reason` is the only thing either audience ever sees.
    assert "systemMessage" not in out


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
    # reshows the verdict the human already reads on systemMessage. Both sides are built from
    # the adverse-only render now — the human's is capped for length, the model's is not, since
    # nothing is competing for that space.
    state.set_relay_enabled(tmp_path, True)
    out = hook.run(_claude_payload(tmp_path))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "✗" in ctx  # the failing check is present, so the agent knows what to fix
    assert "•" not in ctx  # ...but the non-adverse (UNSUPPORTED) lines are not re-listed
    assert "✗" in out["systemMessage"]  # and the human is told what failed


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

def _fake(name: str):
    """The *real* harness record for `name`.

    Was a bare `SimpleNamespace(name=...)`, which is how the relay's per-harness behaviour went
    untested for the thing that mattered: a stub carries whatever the code asks it for, so a unit
    test built on one asserts against a harness that does not exist. These now read the declared
    channels, so a delivery that only works because of how Claude is shaped fails here."""
    from tycho.read import harness as harness_mod

    return harness_mod.BY_NAME[name]


def _verdict(name: str) -> SimpleNamespace:
    """A stand-in verdict. Only `.name` is read on this path."""
    return SimpleNamespace(name=name)


def test_verified_verdict_never_injects_and_clears_streak(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    state.bump_relay_streak(tmp_path)
    out = hook._relay_output(tmp_path, _fake("claude"), _verdict("VERIFIED"), "report", "adverse")
    assert out is None  # nothing to fix — the turn ends
    assert state.relay_streak(tmp_path) == 0


def test_codex_relays_with_block_reason(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    out = hook._relay_output(
        tmp_path, _fake("codex"), _verdict("FAILED"), "full report", "adverse report"
    )
    assert out["decision"] == "block"
    # The human's copy is the full render, same as Claude's — only the delivery differs.
    assert "full report" in out["reason"]
    assert "not a new instruction" in out["reason"]
    assert "systemMessage" not in out  # Codex renders it nowhere — see the end-to-end case
    assert state.relay_streak(tmp_path) == 1


def test_the_verdict_block_caps_the_check_list_and_says_what_it_cut(tmp_path: Path):
    """A ten-check block is half a screen and reads as ten problems. Capped on every harness,
    with the remainder pointed at `tycho show` — Codex is where the cost was visible (the block
    comes back as a full-width message bubble) but it was never cheap on Claude either."""
    report = "\n".join(["🔍 Tycho: INDETERMINATE"] + [f"  • check_{i} — nothing to read" for i in range(9)])
    block = hook._verdict_block(report)
    body = block.splitlines()
    assert body[0] == "🔍 Tycho: INDETERMINATE"
    assert len(body) == hook._BLOCK_MAX_LINES + 2  # header + cap + the "…and N more" line
    assert f"…and {9 - hook._BLOCK_MAX_LINES} more" in body[-1] and "tycho show" in body[-1]
    assert block.count("\n") < report.count("\n")


def test_a_shared_channel_says_it_once_per_turn(tmp_path: Path):
    """Speaking on a shared channel blocks the Stop, and the agent's reply produces another one.

    Observed live on Codex before this: one user turn, two identical blocks, two model replies,
    for a turn that had one thing to report. `stop_hook_active` is the harness saying "this Stop
    *is* that reply". Decay would usually go quiet on the second pass, but usually is not a leash.
    A free human channel has nothing to bound — it costs no turn — so it still speaks.
    """
    from tycho.read import harness as harness_mod

    for name in ("claude", "codex"):
        harness = harness_mod.BY_NAME[name]
        first = hook._speak(harness, "🔍 Tycho: FAILED", continuation=False)
        again = hook._speak(harness, "🔍 Tycho: FAILED", continuation=True)
        assert first is not None, f"{name}: said nothing the first time"
        if harness.channels.human_only:
            assert again is not None, f"{name}: a free channel needs no leash"
        else:
            assert again is None, f"{name}: blocked twice for one turn"


def test_a_finding_is_never_dropped_to_save_space(tmp_path: Path):
    """The cap is for absences, never for findings.

    Every line above is "nothing here to read" and the tenth is worth no more than the fourth.
    A line that names a *failure* is the entire product, so six of them are six lines — hiding
    one to fit a screen is the only trade this code must never make.
    """
    n = hook._BLOCK_MAX_LINES + 3  # more findings than the cap would otherwise allow
    report = "\n".join(["🔍 Tycho: FAILED"] + [f"  ✗ check_{i} — broke" for i in range(n)])
    block = hook._verdict_block(report)
    assert block == report, "a finding was truncated"
    for i in range(n):
        assert f"check_{i}" in block
    assert "tycho show" not in block  # nothing was cut, so nothing to point at


def test_every_harness_shows_the_user_the_same_thing(tmp_path: Path):
    """The parity invariant, and the reason it is a test and not a docstring.

    A person switching harness must read the same words — same verdict block, same pointer at
    how to turn the relay off. What differs is delivery, because the harnesses do: Claude has a
    human field and a model field, Codex has one field that is both. That is a fact about the
    harness; a user noticing it is a bug. Codex went silent for a whole release because this
    lived in prose.
    """
    from tycho.model import CheckResult, CheckStatus, Verdict
    from tycho.views.report import render

    results = [
        CheckResult(name="command_execution", status=CheckStatus.FAIL, evidence="exited 1"),
        CheckResult(name="file_state", status=CheckStatus.PASS, evidence="present on disk"),
    ]
    report = render(Verdict.FAILED, results)
    agent_report = render(Verdict.FAILED, results, only_adverse=True)

    seen = {}
    for name in ("claude", "codex"):
        repo = tmp_path / name
        repo.mkdir()
        state.set_relay_enabled(repo, True)
        out = hook._relay_output(
            repo, _fake(name), Verdict.FAILED, report, agent_report, results
        )
        # Whatever field this harness renders to a person, minus the model's instruction.
        text = out.get("systemMessage") or out["reason"]
        seen[name] = text.split("\n\n[TYCHO] The above")[0]

    assert seen["claude"] == seen["codex"], (
        "the user-facing relay text diverged between harnesses:\n"
        f"claude:\n{seen['claude']}\n\ncodex:\n{seen['codex']}"
    )
    assert "✗ command_execution" in seen["claude"]  # the verdict itself, not just any text
    assert "tycho relay" in seen["claude"]          # ...and how to turn it off


def test_other_harnesses_never_inject_even_when_enabled(tmp_path: Path):
    state.set_relay_enabled(tmp_path, True)
    for h in ("cursor", "opencode"):
        assert hook._relay_output(tmp_path, _fake(h), _verdict("FAILED"), "report", "adverse") is None


def test_disabled_relay_returns_none(tmp_path: Path):
    assert hook._relay_output(tmp_path, _fake("claude"), _verdict("FAILED"), "report", "adverse") is None


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


def test_a_verdict_no_check_names_is_nudged_once_not_three_times(tmp_path: Path):
    """An INDETERMINATE that comes from the combination — files changed, nothing corroborated
    them — hands the agent nothing to fix, so re-asking it the full three times only bought
    three turns of "no fix needed" (and, on Codex, three full-width bubbles). A verdict a check
    can actually name still gets the whole leash."""
    from tycho.model import CheckResult, CheckStatus

    def relay(results):
        return hook._relay_output(
            tmp_path, _fake("codex"), _verdict("INDETERMINATE"), "report", "adverse", results
        )

    quiet = [CheckResult(name="file_state", status=CheckStatus.PASS, evidence="present on disk")]
    state.set_relay_enabled(tmp_path, True)
    assert relay(quiet) is not None      # one nudge: you changed code, nothing verified it
    assert relay(quiet) is None          # …and then the turn is allowed to end

    state.reset_relay_streak(tmp_path)
    named = [CheckResult(name="command_execution", status=CheckStatus.FAIL, evidence="exited 1")]
    assert [relay(named) is not None for _ in range(3)] == [True, True, True]
    assert relay(named) is None          # the full leash, then stop

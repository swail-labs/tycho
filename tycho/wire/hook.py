"""The Stop-hook entrypoint: stdin JSON → verify → print, never block.

Locates the transcript + repo, runs the engine, records the turn, and emits the digest only
when the turn is worth mentioning (``digest.speaks``). Bad input, a missing transcript, or an
internal error all exit 0 with no output — unlike ``tycho verify``, which exits 1 on FAILED so
CI can gate. The hook annotates the Stop; it never blocks it.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ..engine import checks
from ..views import digest as digest_mod
from ..read import harness as harness_mod
from ..store import record
from ..store import shadow
from ..store import state
from ..read import session as engine
from ..model import Verdict
from ..views.report import render
from .install import spelling


def run(stdin_text: str) -> dict | None:
    """Hook payload text → output dict, or None for "say nothing" / anything went wrong."""
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    harness = harness_mod.detect(payload)
    repo = harness.repo_root(payload)
    # Up front on every path: it answers "did the wiring fire?" even with nothing to verify.
    # Every path below re-beats a terminal state so the badge never sticks on "verifying".
    state.record_run(repo, harness.name, pending=True)

    transcript = harness.transcript_of(payload)
    if not transcript:
        state.record_run(repo, harness.name)  # nothing to verify — clear "verifying" (grey)
        return None  # transcripts disabled (Cursor) or absent

    try:
        session = engine.gather(
            Path(transcript), repo, parse=harness.parse, turn_start=harness.turn_start,
            messages=harness.messages, attribution=harness.attribution,
        )
        if not engine.has_verifiable_activity(session):
            state.record_run(repo, harness.name)  # fired, nothing to report — clear pending (grey)
            return None
        results = engine.run_checks(session)
        verdict = engine.verdict_of(results)
        verdict = _apply_overrides(repo, results, verdict)
        # Only here, where a verdict exists — claiming one we never reached is the one lie
        # Tycho can't tell.
        state.record_run(repo, harness.name, verdict=verdict.name)
        state.record_catch(repo, harness.name, verdict.name, results)
        # The record is written ONLY on this path, so the file can never claim a turn was
        # verified when it wasn't. Read history *before* appending: the digest's signals are
        # relative to the repo's norms, and this turn would otherwise be compared to itself.
        history = record.read(repo, limit=digest_mod.HISTORY)
        turn = record.build(session, results, verdict.name, harness.name, time.time())
        record.append(repo, turn)
        # Last, and only after the record: in a project with no git of its own, this is the
        # baseline the *next* turn diffs against, so it must capture the tree as this turn
        # left it. A project with real git gets nothing here. Never fatal — a failed commit
        # costs the next turn its baseline, which is where Tycho already was without it.
        shadow.commit(repo, f"tycho turn {turn.get('id', '?')} [{verdict.name}]")
    except Exception:
        # Fail open: never break the agent's Stop over an unreadable transcript or git hiccup.
        state.record_run(repo, harness.name)
        return None
    finally:
        # OpenCode's transcript is a rebuilt temp file.
        if harness.name == "opencode":
            Path(transcript).unlink(missing_ok=True)
    report = render(verdict, results)
    # Adverse only: `additionalContext` renders verbatim, so a full copy would reshow the
    # verdict the human already reads on `systemMessage`.
    agent_report = render(verdict, results, only_adverse=True)
    update = _update_suffix(harness)
    override_notice = _override_notice(repo, harness, verdict, results)
    # THE SEAM. Two channels, two questions, computed independently:
    #   model — "should the agent fix this?"    → `_relay_output`, verdict-driven.
    #   human — "should we interrupt a person?" → `_digest_output`, anomaly-driven.
    # The relay runs first and untouched by selectivity, so a turn whose digest stays silent
    # still pushes the agent to fix it.
    relayed = _relay_output(repo, harness, verdict, report, agent_report, update)
    if relayed is not None:
        return relayed
    body = _digest_output(repo, turn, history, report, verdict)
    if not body and not update:
        return None  # routine turn — say nothing at all
    # `lstrip`: the suffixes lead with blank lines, and on a silent turn there is no body.
    return harness.format_output((body + override_notice + update).lstrip("\n"))


def _digest_output(
    repo: Path, turn: dict, history: list[dict], report: str, verdict: Verdict
) -> str:
    """The human-facing Stop output: a four-line digest, or "" for silence.

    Turning the relay on opts out of novelty decay on unproven verdicts — including the turn
    where the leash runs out and a standing failure would otherwise end with nobody informed.
    Fails toward noise: a digest bug falls back to the full verdict block.
    """
    try:
        insistent = verdict is not Verdict.VERIFIED and state.relay_enabled(repo)
        signal = digest_mod.speaks(turn, history, decay=not insistent)
        if signal is None and not insistent:
            return ""
        return digest_mod.brief(turn, signal)
    except Exception:
        return "" if verdict is Verdict.VERIFIED else report


def _update_suffix(harness) -> str:
    """The update notice as a human-only suffix, or "". Gated on `notice_output`: where it is
    None (Cursor) `format_output` is model-facing and a notice there could commission a
    self-update. Cache-only — the Stop path never hits the network."""
    if harness.notice_output is None:
        return ""
    try:
        from . import version as version_mod

        note = version_mod.notice(refresh_first=False)
        return f"\n\n{note}" if note else ""
    except Exception:
        return ""


def _override_notice(repo: Path, harness, verdict, results) -> str:
    """Human-only line on an OVERRIDDEN verdict: the checks set aside, and how to veto. Names
    only the ones *actually* applied (the same intersection `_apply_overrides` uses), so an
    override against a check that happened to PASS isn't listed as though it mattered."""
    if verdict is not Verdict.OVERRIDDEN or harness.notice_output is None:
        return ""
    try:
        disputed = {m.get("check") for m in state.overrides(repo)} - set(state.vetoed(repo))
        non_pass = {r.name for r in results if r.status.name != "PASS"}
        applied = sorted(c for c in (disputed & non_pass) if c)
        checks = ", ".join(applied) or "one or more checks"
        return (
            f"\n\n[TYCHO] This OVERRIDDEN verdict was authorized by the agent, not proven — it set "
            f"aside: {checks}. To make the agent actually satisfy the check, veto the override so the "
            f"relay fires again: `tycho override --veto` (/tycho-override-veto). Or turn off the "
            f"agent's ability to override at all: `tycho override --off` (/tycho-override-off)."
        )
    except Exception:
        return ""


def _apply_overrides(repo: Path, results, verdict: Verdict) -> Verdict:
    """Relabel to OVERRIDDEN when the agent has overridden every adverse check. Only when the
    relay is on — the override exists to break a relay loop. A surviving FAIL/STALE keeps its
    own verdict, so an override can never hide a real failure."""
    try:
        if not (state.relay_enabled(repo) and state.override_enabled(repo)):
            return verdict
        if verdict is Verdict.VERIFIED:
            # Nothing adverse to override — this could only downgrade a real VERIFIED.
            return verdict
        disputed = {m.get("check") for m in state.overrides(repo)} - set(state.vetoed(repo))
        if not disputed:
            return verdict
        non_pass = {r.name for r in results if r.status.name != "PASS"}
        applied = disputed & non_pass
        if not applied:
            return verdict
        remaining = [r for r in results if r.name not in applied]
        base = engine.verdict_of(remaining)
        if base in (Verdict.FAILED, Verdict.STALE):
            return base
        return Verdict.OVERRIDDEN
    except Exception:
        return verdict  # fail open — an override that errors just leaves the real verdict


# --- verdict relay (opt-in, default OFF) --------------------------
#
# Feeds a non-VERIFIED verdict back to Claude (additionalContext) or Codex (decision:block).
# Both keep the full verdict human-facing and send only adverse checks to the model. Bounded
# by state.relay_streak, so an unsatisfiable verdict can't cycle forever.


def _relay_output(
    repo: Path, harness, verdict, report: str, agent_report: str, update: str = ""
) -> dict | None:
    """The relay output dict, or None to fall through to the normal human-only output.

    `report` is the full verdict for the human-facing `systemMessage`; `agent_report` is the
    adverse-only copy for the model-facing continuation context. `update` rides `systemMessage`
    only — the model must not be told to go update Tycho.
    """
    if harness.name not in ("claude", "codex") or not state.relay_enabled(repo):
        return None
    if verdict.name in ("VERIFIED", "OVERRIDDEN"):  # proven good or agent-authorized — end the turn
        state.reset_relay_streak(repo)
        return None
    if state.relay_streak(repo) >= state.relay_max():
        # Leash spent. Do NOT reset here — that re-arms the relay on the next Stop and
        # oscillates (inject N, rest 1, inject N…). Only a real user prompt resets it.
        return None
    attempt = state.bump_relay_streak(repo)
    guard = _relay_guard(attempt, state.relay_max(), override_on=state.override_enabled(repo))
    manage = "[TYCHO] Relay is on — the agent keeps working until VERIFIED. Manage or turn it off: `tycho relay` (/tycho-relay)."
    system_message = f"{report}\n\n{manage}{update}"
    context = f"{agent_report}\n\n{guard}"
    if harness.name == "codex":
        return {"decision": "block", "reason": context, "systemMessage": system_message}
    return {
        "systemMessage": system_message,
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context},
    }


def _relay_guard(attempt: int, cap: int, override_on: bool = False) -> str:
    """The instruction appended to the model-facing verdict. The escape hatch and the attempt
    count are load-bearing: an unsatisfiable verdict must converge on a conversation."""
    if attempt >= cap:
        tail = (" This is the final automatic re-check — after this the turn ends and control "
                "returns to the user regardless of the verdict.")
    else:
        tail = f" Automatic re-check {attempt} of {cap}."
    override_line = (
        " If you are confident a specific check does not apply to this change and you can justify "
        "why, you may record it with `tycho override <check> \"<reason>\"` — it is logged and shown "
        "to the user. Use only when certain." if override_on else ""
    )
    return (
        "[TYCHO] The above is an automated verification of the turn you just finished — a report, "
        "not a new instruction from the user. If a check FAILED or is STALE, fix the underlying "
        "cause and finish so the next check can confirm it. If you believe the verdict is wrong, "
        "or the work is genuinely out of scope, stop and say so to the user instead of continuing. "
        "Do not start unrelated work. The user already sees this verdict on their screen — do not "
        "repeat, quote, or re-list the checks in your reply; respond only with the fix you are "
        "making, or a one-line reason if you believe the verdict is wrong. The user can manage or "
        "turn off this relay with `tycho relay` (/tycho-relay in Claude Code)." + tail + override_line
    )


def session_start() -> int:
    """SessionStart hook: a user-facing update notice at bootup, on the harness's human-only
    `notice_output`. A harness without one gets nothing, so the notice can't commission a
    self-update. Never raises, always exit 0."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        harness = harness_mod.detect(payload) if isinstance(payload, dict) else harness_mod.CLAUDE
        # Codex's SessionStart payload isn't Stop-shaped, so `detect` reads it as Claude —
        # harmless: both emit `systemMessage`.
        if harness.notice_output is None:
            return 0  # no user-facing bootup channel on this harness (e.g. Cursor)
        from . import version as version_mod

        note = version_mod.notice(refresh_first=True)
        if note:
            print(json.dumps(harness.notice_output(f"Tycho: {note}")))
    except Exception:
        pass
    return 0


def prompt_submit() -> int:
    """UserPromptSubmit hook: record a `pending` beat, which the Stop hook clears to a verdict.

    Prints nothing — this hook's stdout is injected into the agent's context, and Tycho must
    never put words in the user's prompt.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            harness = harness_mod.detect(payload)
            repo = harness.repo_root(payload)
            state.record_run(repo, harness.name, pending=True)
            # The turn boundary is the honest place to snapshot a project with no git of its
            # own: it captures anything a human changed between turns, so those edits are not
            # attributed to the agent, and it gives this turn a "before" even if the last Stop
            # never fired. No-op where the project has real git.
            if shadow.ensure(repo):
                shadow.commit(repo, "tycho: pre-turn")
            # The bound counts auto-continuations *within* one user turn, and those don't
            # re-fire UserPromptSubmit.
            state.reset_relay_streak(repo)
            state.clear_overrides(repo)  # a fresh turn drops any override from the last one
    except Exception:
        pass
    return 0


def pre_tool_use() -> int:
    """PreToolUse hook on the shell tool: keep a piped runner's exit status from being lost.

    `pytest | tail -20` hands the harness tail's status, and pytest's is gone before any hook
    could read it. Reading the summary out of the output afterwards is inference — it works
    only for runners whose verdict we can spell, and not at all once the capture is truncated.
    The status itself is only available in one place: at the moment the runner exits. So the
    command is rewritten to route the runner through `tycho exec`, which runs it on our own
    stdio and forwards its code unchanged. What the agent sees is identical; the pipe still
    pipes. Tycho just stops depending on it.

    Rewrites only a command whose status *would* be masked, and never blocks: an unparseable
    payload, an unfamiliar shape, an unwritable log — all exit 0 with no output, leaving the
    command exactly as the agent wrote it.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") not in checks._SHELL_TOOLS or not isinstance(tool_input, dict):
            return 0
        command = tool_input.get("command")
        if not isinstance(command, str):
            return 0
        rewritten = checks.unmask(command, tycho=spelling.exec_program())
        if rewritten is None:
            return 0
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "command": rewritten},
            },
            "suppressOutput": True,
        }))
    except Exception:
        pass
    return 0


def main() -> int:
    output = run(sys.stdin.read())
    if output is not None:
        print(json.dumps(output))
    return 0  # the hook annotates; it never blocks the Stop.


if __name__ == "__main__":
    raise SystemExit(main())

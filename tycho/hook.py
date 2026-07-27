"""The Stop-hook entrypoint: stdin JSON → verify → print, never block.

Wired by ``tycho init`` (next milestone) into the harness's Stop hook. Reads the
hook payload on stdin, locates the transcript + repo, runs the engine, records the
turn, and emits — *when the turn is worth mentioning* — the digest as the harness's
JSON output. Most turns emit nothing: see ``_digest_output`` and ``digest.speaks``.

A verifier must never break the agent: any bad input, missing transcript, or
internal error exits 0 with no output. Unlike ``tycho verify`` (which exits 1 on
FAILED so CI can gate), the *hook* always exits 0 — it annotates the Stop, it
never blocks it. That is a design invariant: Tycho never blocks.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import digest as digest_mod
from . import harness as harness_mod
from . import record
from . import state
from . import verify as engine
from .model import Verdict
from .report import render


def run(stdin_text: str) -> dict | None:
    """Pure-ish core: hook payload text → output dict (or None for "say nothing").

    Split from stdin/stdout so it's testable without patching the process.
    Returns None whenever there is nothing to report or anything goes wrong.
    """
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    harness = harness_mod.detect(payload)
    repo = harness.repo_root(payload)
    # Heartbeat, up front on every path: it answers "did the wiring fire?" — the basis of
    # `tycho doctor` (TYCHO-8) — so it must land even when there's nothing to verify.
    # `pending=True` shows the badge "verifying"; every path below re-beats a terminal state
    # so it never sticks (TYCHO-59). Never raises.
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
        # Re-beat with the verdict, for `tycho status` to render passively (TYCHO-39).
        # Only here, where a verdict exists: the entry beat above already proved liveness,
        # and claiming a verdict we never reached would be the one lie Tycho can't tell.
        state.record_run(repo, harness.name, verdict=verdict.name)
        # And log it to the catch record with its evidence trail, if adverse/intermediate
        # (TYCHO-62). No-op for VERIFIED/UNSUPPORTED; never raises.
        state.record_catch(repo, harness.name, verdict.name, results)
        # And the durable per-turn record (strategy §9.2) — the substrate `tycho blame`,
        # the turn digest, the attestation trailer and the decay ledger all read. Written
        # ONLY here, on the one path that reached a real verdict: every earlier return
        # above (nothing to verify, no transcript, unreadable session) writes no record, so
        # the file can never claim a turn was verified when it wasn't. `append` never
        # raises; `build` is pure, so the clock is read here and passed in.
        # Read the repo's prior turns *before* appending this one: the digest's signals are
        # relative to what this repo normally does, and a history containing the turn being
        # judged would compare it against itself (a wide turn would raise its own norm, a
        # standing failure would decay itself into silence on its first occurrence).
        history = record.read(repo, limit=digest_mod.HISTORY)
        turn = record.build(session, results, verdict.name, harness.name, time.time())
        record.append(repo, turn)
    except Exception:
        # broad catch is the correct behavior here — fail open, never
        # break the agent's Stop over an unreadable transcript or git hiccup. Clear the
        # pending beat so the badge doesn't sit on "verifying" forever.
        state.record_run(repo, harness.name)
        return None
    finally:
        # OpenCode's transcript is a rebuilt temp file — clean it up either way.
        if harness.name == "opencode":
            Path(transcript).unlink(missing_ok=True)
    report = render(verdict, results)
    # The model-facing relay copy carries only the adverse checks: the harness renders
    # `additionalContext` verbatim as "Stop hook feedback", so a full copy there would reshow
    # the whole verdict the human already reads on `systemMessage` (point-1 duplication).
    agent_report = render(verdict, results, only_adverse=True)
    # A human-only "newer Tycho available" line, appended to the verdict the user is already
    # reading (TYCHO-116). Cache-only — never a network call on the hot Stop path.
    update = _update_suffix(harness)
    override_notice = _override_notice(repo, harness, verdict, results)
    # THE SEAM (strategy §9.1 vs §11.1). Two channels, two different questions:
    #
    #   model channel  — "should the agent fix this?"     → `_relay_output`, verdict-driven.
    #   human channel  — "should we interrupt a person?"  → `_digest_output`, anomaly-driven.
    #
    # They are computed independently and on purpose. The relay is the *first* branch and is
    # untouched by selectivity: it still fires on every non-VERIFIED verdict, so a turn whose
    # digest stays silent (an INDETERMINATE that isn't news, a failure that has been standing
    # for three turns) still pushes the agent to fix it. The digest never writes into
    # `additionalContext` / `reason`; the relay never consults `digest.speaks`.
    relayed = _relay_output(repo, harness, verdict, report, agent_report, update)
    if relayed is not None:
        return relayed
    body = _digest_output(repo, turn, history, report, verdict)
    if not body and not update:
        return None  # routine turn — say nothing at all (§11.1: talk less, be read)
    # `lstrip` because `update`/`override_notice` lead with blank lines to separate them from a
    # body that, on a silent turn, isn't there.
    return harness.format_output((body + override_notice + update).lstrip("\n"))


def _digest_output(
    repo: Path, turn: dict, history: list[dict], report: str, verdict: Verdict
) -> str:
    """The human-facing Stop output: a four-line digest, or "" for silence (strategy §11.1).

    Selectivity lives in `digest.speaks`, which reads this repo's own recent turns — so the
    hook stays quiet on turns that are normal *here* and speaks on the ones that aren't.

    **The relay opt-in overrides the silence, and only for unproven verdicts.** Turning the
    relay on is an operator saying "keep the agent working until this is VERIFIED", so on a
    non-VERIFIED turn they get told every time — including the turn where the relay itself goes
    quiet because the leash ran out, which is precisely when a standing failure would otherwise
    end the turn with nobody informed. Novelty decay is the *human's* filter; the relay opt-in
    is the human electing out of it. With the relay off (the default) nothing changes.

    Never raises, and fails toward the old behaviour rather than toward silence: a digest bug on
    an unproven turn falls back to the full verdict block, because losing an adverse verdict to
    a rendering error is the one outcome worse than being noisy. A routine turn stays silent
    either way — there was nothing to lose.
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
    """The Stop-hook update notice as a human-only suffix (leading blank line), or "" (TYCHO-116).

    Only on harnesses with a human-only Stop channel: there, `format_output` writes the same field
    `notice_output` does (systemMessage / message), so appending here stays human-facing. On Cursor
    `format_output` is model-facing (`followup_message`) and `notice_output is None`, so a notice
    would reach the model (TYCHO-35's rule) — suppress it, exactly as the bootup notice does.

    Cache-only (`refresh_first=False`): the Stop path must never hit the network — the once-a-day
    fetch is the doctor/init/bootup job. Respects opt-out + dismissal via `version.notice`. Never
    raises: an update line is never worth breaking a Stop over."""
    if harness.notice_output is None:
        return ""
    try:
        from . import version as version_mod

        note = version_mod.notice(refresh_first=False)
        return f"\n\n{note}" if note else ""
    except Exception:
        return ""


def _override_notice(repo: Path, harness, verdict, results) -> str:
    """Human-only line on an OVERRIDDEN verdict: name the checks the agent set aside and
    tell the user how to veto (relay fires again) or turn override off. Same human-only gate
    as `_update_suffix` — never emitted on a model-facing channel (TYCHO-35). Never raises.

    Names only the checks that were *actually* set aside — the same `disputed & non-PASS`
    intersection `_apply_overrides` applies — so an override recorded against a check that
    happened to PASS (a no-op for the verdict) is never listed as though it changed it."""
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
    """Relabel `verdict` to OVERRIDDEN when the agent has overridden every adverse check.

    Only when the relay is on AND the capability is on — the override exists to break a relay
    loop, so with the relay off there is nothing to break and the real verdict stands. Drops each
    overridden check that is currently non-PASS (a PASS/absent name is a no-op), recomputes from
    the rest, and returns OVERRIDDEN iff no adverse (FAILED/STALE) check survives. A surviving
    FAIL/STALE keeps its verdict so the relay keeps firing — an override can never hide a real
    failure. Never raises."""
    try:
        if not (state.relay_enabled(repo) and state.override_enabled(repo)):
            return verdict
        if verdict is Verdict.VERIFIED:
            # TYCHO-119: a proven-green turn has no adverse (FAIL/STALE) check to override, so an override
            # here could only downgrade a real VERIFIED to OVERRIDDEN. Leave it. Non-VERIFIED verdicts
            # (including INDETERMINATE) still reach OVERRIDDEN below — the escape hatch is unchanged.
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


# --- verdict relay (TYCHO-35, opt-in, default OFF) --------------------------
#
# When the operator turns the relay on, feed a non-VERIFIED verdict back to Claude or Codex.
# Claude continues from Stop-hook additionalContext; Codex continues from decision:block + reason.
# Both transports keep the full verdict human-facing and send only adverse checks to the model.
#
# It is *bounded* by state.relay_streak: at most relay_max() auto-continuations per user turn,
# after which Tycho goes quiet and the turn ends normally — an unsatisfiable verdict can't cycle
# forever. Off by default, so every other harness and the un-opted-in path emit
# exactly the human-only output they always did (no agent context used, no extra generations).


def _relay_output(
    repo: Path, harness, verdict, report: str, agent_report: str, update: str = ""
) -> dict | None:
    """The relay output dict, or None to fall through to the normal human-only output.

    Returns None (unchanged behaviour) unless the relay is enabled here, the harness is Claude
    or Codex, and the verdict is worth acting on. VERIFIED clears the streak and ends the turn
    (nothing to fix); once the streak reaches relay_max() the relay goes quiet.

    `report` is the full verdict (every check) for the human-facing `systemMessage`; `agent_report`
    is the adverse-only copy for the model-facing continuation context.

    `update` is the human-only update line (TYCHO-116): it rides the human-facing `systemMessage`
    only, never the continuation context — the model must not be told to go update Tycho.
    """
    if harness.name not in ("claude", "codex") or not state.relay_enabled(repo):
        return None
    if verdict.name in ("VERIFIED", "OVERRIDDEN"):  # proven good or agent-authorized — end the turn
        state.reset_relay_streak(repo)
        return None
    if state.relay_streak(repo) >= state.relay_max():
        # Leash spent for this user turn — go quiet and let the turn end. Deliberately do NOT
        # reset here: resetting would re-arm the relay on the very next Stop and oscillate
        # (inject N, rest 1, inject N…) instead of stopping. The streak stays maxed until a real
        # user prompt (prompt_submit) resets it — that is what scopes the bound to one user turn.
        return None
    attempt = state.bump_relay_streak(repo)
    guard = _relay_guard(attempt, state.relay_max(), override_on=state.override_enabled(repo))
    # systemMessage keeps the human seeing the verdict, now with a pointer to manage/turn off the
    # relay (TYCHO-114); additionalContext is the model-facing copy that also drives the continuation.
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
    """The instruction appended to the model-facing verdict. Frames it as a report, points at
    fixing the root cause, and — crucially — offers the escape hatch of stopping to tell the
    user, so a verdict the agent can't satisfy converges on a conversation rather than a loop.
    Names the attempt count so the model knows the leash is finite."""
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
    """SessionStart / bootup hook: surface a *user-facing* update notice at agent bootup
    (TYCHO-53, generalized to Codex/OpenCode in TYCHO-72). The output shape is the harness's
    `notice_output` — a human-only channel (`systemMessage` on Claude/Codex, `message` toasted
    by the OpenCode plugin). A harness with no such channel (Cursor: `notice_output is None`)
    gets nothing, so the notice can never reach the model and commission a self-update
    (TYCHO-35's rule). We parse the payload only to pick that shape; the update check itself is
    machine-global, not per-repo.

    Same invariants as the Stop hook: never raises, always exit 0, prints nothing when
    offline / opted out / already current. A bootup hook must never break the session.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        harness = harness_mod.detect(payload) if isinstance(payload, dict) else harness_mod.CLAUDE
        # Codex's SessionStart payload isn't Stop-shaped so `detect` reads it as Claude — which
        # is harmless here: both emit `systemMessage`, so the notice shape is identical either way.
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
    """UserPromptSubmit hook (Claude Code): the user just submitted a prompt, so a turn is
    starting. Record a `pending` beat so the status badge shows frost-blue "verifying" for the
    whole run (TYCHO-94); the Stop hook clears it to the verdict when the turn ends.

    Prints nothing — a UserPromptSubmit hook's stdout is injected into the agent's context, and
    Tycho must never put words in the user's prompt. Same invariants as every hook: never
    raises, always exit 0, touches only the heartbeat.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            harness = harness_mod.detect(payload)
            repo = harness.repo_root(payload)
            state.record_run(repo, harness.name, pending=True)
            # A real user prompt opens a fresh turn, so the verdict-relay leash resets here: the
            # bound (TYCHO-35) counts auto-continuations *within* one user turn, not across them.
            # Auto-continuations don't re-fire UserPromptSubmit, so this only ever runs for the
            # human's own prompts — exactly the boundary the bound is scoped to.
            state.reset_relay_streak(repo)
            state.clear_overrides(repo)  # a fresh turn drops any override from the last one
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

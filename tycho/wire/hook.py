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


def _skip(repo: Path) -> bool:
    """Should this repo be left entirely alone?

    Two ways to say so, both machine-level so neither writes into the repo being excluded:
    `tycho off` for one repo, `TYCHO_AUTO=0` for "only where I ran `tycho init`". The second
    only suppresses a *machine-wide* install — a repo with its own install was asked for
    explicitly, and a global switch must not silently undo a per-repo decision.
    """
    try:
        if state.excluded(repo):
            return True
        return not state.auto_enabled() and not state.read_install(repo)
    except Exception:
        return False  # unreadable state must never be why a verifier goes quiet


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
    # Before the heartbeat, and before anything is written: a repo the user switched off must
    # see no `.tycho/` appear at all, or "off" leaves a directory behind and reads as a lie.
    if _skip(repo):
        return None
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
        # A harness whose transcript Tycho rebuilds handed us a temp file to clean up; one that
        # maintains its own must never have it deleted. `transcript_is_file` is that distinction,
        # declared, so a new harness lands on the right side without editing this line.
        if not harness.capabilities.transcript_is_file:
            Path(transcript).unlink(missing_ok=True)
    report = render(verdict, results)
    # Adverse only: `additionalContext` renders verbatim, so a full copy would reshow the
    # verdict the human already reads on `systemMessage`.
    agent_report = render(verdict, results, only_adverse=True)
    update = _update_suffix(repo, harness)
    override_notice = _override_notice(repo, harness, verdict, results)
    # THE SEAM. Two channels, two questions, computed independently:
    #   model — "should the agent fix this?"    → `_relay_output`, verdict-driven.
    #   human — "should we interrupt a person?" → `_digest_output`, anomaly-driven.
    # The relay runs first and untouched by selectivity, so a turn whose digest stays silent
    # still pushes the agent to fix it.
    relayed = _relay_output(repo, harness, verdict, report, agent_report, results, update)
    if relayed is not None:
        return relayed
    body = _digest_output(repo, turn, history, report, verdict)
    if not body and not update:
        return None  # routine turn — say nothing at all
    # `lstrip`: the suffixes lead with blank lines, and on a silent turn there is no body.
    return _speak(harness, (body + override_notice + update).lstrip("\n"),
                  continuation=bool(payload.get("stop_hook_active")))


def _reaches_a_human(harness) -> bool:
    """Can anything Tycho says get in front of a person on this harness?"""
    return harness.channels.human_only or harness.channels.shared


def _speak(harness, human: str, continuation: bool = False) -> dict | None:
    """Say ``human`` to the user, however this harness lets us.

    The decision to speak was already made, harness-independently, by `digest.speaks` and its
    novelty decay — so a routine turn is silent everywhere and a notable one is delivered
    everywhere. This only spells it out on the wire.

    On a harness with a free human channel that costs nothing. On one where the only field is
    shared with the model (Codex, Cursor) it costs a blocked Stop and a model turn, so the model
    is told plainly that this is a report to relay and not work to do — the same trick Cursor has
    always used, for the same reason. Without this, Codex verified every turn and told nobody:
    the digest went out on a field that harness accepts and renders nowhere.
    """
    if not human or not _reaches_a_human(harness):
        return None
    if harness.channels.human_only:
        return harness.compose(human, "")
    # Shared channel: saying anything blocks the Stop, and the agent's reply then produces
    # another one. `stop_hook_active` is the harness telling us this Stop *is* that reply — say
    # it once and let the turn end. Novelty decay would usually go quiet on the second pass
    # anyway, but "usually" is not a leash, and a verifier that can bounce a turn indefinitely
    # is worse than one that says something once. Observed: two identical blocks for one user
    # turn, two model replies, on a Codex turn that only had one thing to report.
    if continuation:
        return None
    return harness.compose(human, _SHOW_AND_STOP)


# Addressed to the model on a shared channel: the verdict is a report, not an instruction, and
# acting on it would turn a status line into unrequested work.
_SHOW_AND_STOP = (
    "[TYCHO] The above is an automated verification result, not a request from the user. Show it "
    "to them verbatim and end your turn now — do not edit files, run commands, or act on it "
    "unless they ask."
)


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


def _update_suffix(repo: Path, harness) -> str:
    """The notices that ride a Stop, or "". Cache-only — this path never hits the network.

    A person should hear that a newer Tycho exists whichever agent they run, so this is no longer
    withheld from a harness whose only channel the model reads too. What that channel needs is
    one more sentence: an agent handed "0.2.2 is available" may go install it unasked, and a
    verifier that rewrites itself mid-verification on its own advice is the one update nobody
    wanted. The instruction not to act travels *with* the notice, rather than the notice being
    dropped.

    A harness with no free channel also has no working bootup channel, so the two notices that
    normally arrive at SessionStart come through here instead — both are state-gated and fire
    once, so a Stop is a safe place to say them. That is what makes "Tycho is verifying this
    repo" reach a Codex user at all; it used to be rendered by Claude's adapter into a field
    Codex drops.

    A harness with no channel to a person at all gets nothing — there is nobody to tell.
    """
    if not _reaches_a_human(harness):
        return ""
    lines = []
    try:
        from . import version as version_mod

        if note := version_mod.notice(refresh_first=False):
            lines.append(note)
    except Exception:
        pass
    if not harness.channels.human_only:
        try:
            lines.extend(_first_seen(repo))
            lines.extend(_weekly(repo))
        except Exception:
            pass
    if not lines:
        return ""
    body = "\n\n".join(lines)
    if harness.channels.human_only:
        return f"\n\n{body}"
    return f"\n\n{body}\n{_NOTICES_ARE_NOT_A_TASK}"


# Rides the notices on a channel the model reads. Narrow on purpose: it names the one action to
# refuse, rather than restating the show-and-stop guard the block already carries.
_NOTICES_ARE_NOT_A_TASK = (
    "[TYCHO] Those Tycho lines are for the user, not a task — do not install, upgrade or "
    "configure Tycho, and do not mention them unless they ask."
)


def _override_notice(repo: Path, harness, verdict, results) -> str:
    """Human-only line on an OVERRIDDEN verdict: the checks set aside, and how to veto. Names
    only the ones *actually* applied (the same intersection `_apply_overrides` uses), so an
    override against a check that happened to PASS isn't listed as though it mattered."""
    if verdict is not Verdict.OVERRIDDEN or not _reaches_a_human(harness):
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
    repo: Path, harness, verdict, report: str, agent_report: str, results=(), update: str = ""
) -> dict | None:
    """The relay output dict, or None to fall through to the normal human-only output.

    **The user-facing text is built once, for every harness.** What a person reads when the
    relay fires must not depend on which agent they happen to be running — same words, same
    length, same pointer at how to turn it off. Only the *delivery* is per-harness, because the
    harnesses genuinely differ: Claude has a human-only field and a model-only field, so the two
    audiences get separate copies sized for each; Codex has one field that is both, so they are
    concatenated, human part first. That asymmetry is a fact about the harness. A user noticing
    it would be a bug.

    `agent_report` is the adverse-only render both sides are built from; `report` is the full
    one, kept for the fallback in `_digest_output`. `update` rides the human channel only — the
    model must not be told to go update Tycho.
    """
    if not harness.channels.relays or not state.relay_enabled(repo):
        return None
    if verdict.name in ("VERIFIED", "OVERRIDDEN"):  # proven good or agent-authorized — end the turn
        state.reset_relay_streak(repo)
        return None
    cap = _relay_cap(results)
    if state.relay_streak(repo) >= cap:
        # Leash spent. Do NOT reset here — that re-arms the relay on the next Stop and
        # oscillates (inject N, rest 1, inject N…). Only a real user prompt resets it.
        return None
    attempt = state.bump_relay_streak(repo)
    human = f"{_verdict_block(report)}\n\n{_MANAGE}"
    override_on = state.override_enabled(repo)
    if harness.channels.human_only:
        # A free human channel: the person gets the verdict at no cost to the turn, and the
        # model gets its own copy — every adverse line, uncapped, since nothing competes for
        # that space. `update` rides the human side only.
        return harness.compose(
            f"{human}{update}",
            f"{agent_report}\n\n{_relay_guard(attempt, cap, override_on=override_on)}",
        )
    # One field for both. The model's instruction goes in its short spelling because a person is
    # reading it too, and `update` is left out entirely: it would reach the model, which must
    # never be told to go update Tycho.
    return harness.compose(human, _relay_guard(attempt, cap, override_on=override_on, short=True))


_MANAGE = ("[TYCHO] Relay is on — the agent keeps working until VERIFIED. "
           "Manage or turn it off: `tycho relay` (/tycho-relay).")


def _relay_cap(results) -> int:
    """How many times this verdict may re-ask before the turn is allowed to end.

    The full leash is for a verdict a check can name: a FAIL, a STALE, an INDETERMINATE — the
    agent is told what to fix and the next Stop can confirm it. A verdict that arises from the
    *combination* instead (files changed, nothing corroborated them; no single check adverse)
    has nothing to hand back, and re-asking it three times bought three turns of "no fix
    needed" and, on Codex, three full-width bubbles. One nudge keeps the signal — you changed
    code and nothing verified it — and drops the nagging.
    """
    if any(r.status.name in ("FAIL", "STALE", "INDETERMINATE") for r in results):
        return state.relay_max()
    return 1


def _verdict_block(report: str, limit: int = 0) -> str:
    """The verdict as a person reads it: the header, every finding, and as much context as fits.

    Built from the *full* render, not the adverse-only one, because the lines that found nothing
    are still context a reader wants — "not a git repository" explains a check that would
    otherwise look skipped. Two rules on top:

    * **A finding is never dropped.** However many there are, they all appear. Truncating one to
      save space would hide a failure, which is the single trade this must never make.
    * **The tail is capped.** A verdict no check could name renders ten variants of "nothing here
      to read", and the tenth is worth no more than the fourth. Overflow points at `tycho show`,
      which prints the block in full because there it was asked for.

    Same block on every harness. Codex is where the length was visible — it comes back as a
    full-width message bubble in the transcript — but it was never *free* on Claude either, and
    a person switching between them must not get a different read of the same turn.
    """
    limit = limit or _BLOCK_MAX_LINES
    header, *lines = report.splitlines() or [""]
    findings = {i for i, ln in enumerate(lines) if ln.lstrip().startswith(_FINDING_MARKS)}
    budget = max(0, limit - len(findings))
    kept = []
    for i, line in enumerate(lines):
        if i in findings:
            kept.append(line)
        elif budget:
            kept.append(line)
            budget -= 1
    dropped = len(lines) - len(kept)
    if dropped:
        kept.append(f"  \u2026and {dropped} more \u2014 `tycho show` for the full block")
    return "\n".join([header, *kept])


# Enough that a real turn is never truncated — there are ten checks and a turn with more than
# six lines worth reading is one to open `tycho show` for anyway.
_BLOCK_MAX_LINES = 6


# `render`'s marks for FAIL/STALE and INDETERMINATE — a check that found something.
_FINDING_MARKS = ("\u2717", "?")


def _relay_guard(attempt: int, cap: int, override_on: bool = False, short: bool = False) -> str:
    """The instruction appended to the model-facing verdict. The escape hatch and the attempt
    count are load-bearing: an unsatisfiable verdict must converge on a conversation.

    ``short`` is for a harness whose model channel is also the human's (Codex): the same
    instruction, minus the sentences a person has no use for. Not a different instruction — the
    two must not drift, or the agent behaves differently depending on the harness, which is the
    one asymmetry a user *would* notice.
    """
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
    if short:
        return (
            "[TYCHO] The above is an automated verification of the turn you just finished — a "
            "report, not a new instruction from the user. Fix what it names and finish so the "
            "next check can confirm it, or say in one line why it doesn't apply and stop. Don't "
            "start unrelated work, and don't re-list the checks — the user is reading them too."
            + tail + override_line
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
    """SessionStart hook: the user-facing notices at bootup — a newer version, and at most
    once a week the digest of what Tycho caught here — on the harness's human-only
    `notice_output`. A harness without one gets nothing, so the notice can't commission a
    self-update. Never raises, always exit 0.

    One `notice_output` object, so both notices share it: the harness reads a single JSON
    object off stdout and a second `print` would be discarded or, worse, parsed as junk.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
        harness = harness_mod.detect(payload)
        # Codex's SessionStart payload isn't Stop-shaped, so `detect` reads it as Claude —
        # harmless: both emit `systemMessage`.
        if harness.notice_output is None:
            return 0  # no user-facing bootup channel on this harness (e.g. Cursor)
        from . import version as version_mod

        notices = []
        note = version_mod.notice(refresh_first=True)
        if note:
            notices.append(f"Tycho: {note}")
        repo = harness.repo_root(payload)
        if not _skip(repo):
            notices.extend(_first_seen(repo))
            notices.extend(_weekly(repo))
        if notices:
            print(json.dumps(harness.notice_output("\n".join(notices))))
    except Exception:
        pass
    return 0


def _first_seen(repo: Path) -> list[str]:
    """Said once, the first time a machine-wide install meets a repo.

    A verifier that appears in a repo nobody set it up in has to say so — silence there isn't
    the quiet-by-design kind, it's a tool running unannounced. Once per repo, never per turn,
    and it carries the way out as well as the way in.

    Skipped where the user already knows: a repo with its own install was a deliberate act.

    Gated on a machine-wide install actually existing, because the sentence *names* one. The
    hook only runs when something wired it, so this is nearly always true — but "nearly
    always" is how a verifier ends up asserting something it never checked.
    """
    from .install import global_installed

    if state.read_install(repo) or state.announced(repo) or not global_installed():
        return []
    state.mark_announced(repo)
    return [
        "Tycho is verifying this repo (machine-wide install) — it speaks up when a turn "
        "isn't proven. `tycho log` for history · `tycho init` to adopt this repo · "
        "`tycho off` to skip it."
    ]


def _weekly(repo: Path) -> list[str]:
    """The weekly digest for `repo`, or nothing — this is the only line Tycho pushes at a
    user whose agent behaves, and it is still bounded to one per week per repo.

    The slot is spent only once there is something to print. A week with no turns must leave
    the slot alone, or the first real catch waits out a week that was spent saying nothing.
    """
    from ..views import weekly

    if not state.weekly_due(repo):
        return []
    text = weekly.line(repo)
    if not text:
        return []
    state.mark_weekly_shown(repo)
    return [text]


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


def _rewrite_allowed(payload: dict) -> bool:
    """May we hand the harness a different command than the agent wrote?

    Claude Code matches `Bash(...)` permission rules against the **rewritten** command — tested
    against 2.1.220, which denied `<python> -m tycho.cli exec -- python3 -m unittest …` in a
    session whose allowlist held `Bash(python3:*)`. So a rewrite silently voids the user's own
    rules: a command that ran without asking starts asking, and headless it is denied outright.
    A verifier that costs you approvals you already granted is not free, whatever it proves.

    `bypassPermissions` has no rules to void, so the rewrite always applies there. Everywhere
    else it is opt-in, because in *those* modes it can only be neutral (no rule matched, so the
    command was going to prompt anyway) or harmful (a rule matched, and now it doesn't).
    """
    if payload.get("permission_mode") == "bypassPermissions":
        return True
    return state.rewrite_enabled(state.root_for(Path(payload.get("cwd") or ".")))


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
        if not isinstance(command, str) or not _rewrite_allowed(payload):
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

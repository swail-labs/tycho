"""`command_execution` — the check that reads a test/build run's own verdict."""

from __future__ import annotations

from ...model import CheckResult, CheckStatus, Session
from .cmdread import _runner_segment
from .common import _r, _scope, _short
from .outcome import (
    _exec_run_for, _outcome, _runner_events, _status_is_masked, _unresolved_reds, _was_refused,
)


def command_execution(session: Session) -> CheckResult:
    runners = _runner_events(session.turn_events)
    if not runners:
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"no test/build command ran this {_scope(session)}",
        )
    last = max(runners, key=lambda e: e.ts)
    raw = last.input.get("command", "")
    cmd = _short(_runner_segment(raw) or raw)
    masked = _status_is_masked(raw)
    run = _exec_run_for(last, session.commands)
    outcome = _outcome(last, session.commands)
    if outcome is None:
        # A refused command never reached the shell, so "ran but…" would be the receipt's own
        # small lie — and the remedy is approval, not `tycho exec`.
        if _was_refused(last):
            return _r(
                "command_execution",
                CheckStatus.UNSUPPORTED,
                f"`{cmd}` was never run — the harness refused it (permission not granted),"
                " so there is no result to check",
            )
        why = (
            "its exit status was masked by the shell" if masked
            else "no exit status was recorded"
        )
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"`{cmd}` ran but {why}, and its output carries no summary — Tycho can't confirm it passed"
            " (prefix it with `tycho exec --` to put its real status on the record)",
        )
    # Name the source: output-recovered evidence is weaker than an exit code.
    if run is not None:
        via = f" (Tycho ran it — exit {run.exit_code})"
    else:
        via = " (read from its output — exit status masked by the shell)" if masked else ""
    if outcome:
        return _r("command_execution", CheckStatus.FAIL, f"`{cmd}` ran but reported an error{via}")
    unresolved = [e for e in _unresolved_reds(session.turn_events, session.commands) if e.ts < last.ts]
    if unresolved:
        red = _short(_runner_segment(unresolved[0].input.get("command", "")) or "")
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"`{red}` reported an error earlier this {_scope(session)} and was never re-run —"
            f" `{cmd}` passed but is a different command, so it can't stand in for it",
        )
    return _r("command_execution", CheckStatus.PASS, f"`{cmd}` ran without error{via}")

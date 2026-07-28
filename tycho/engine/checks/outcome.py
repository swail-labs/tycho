"""Did a runner invocation pass? One predicate — `_outcome` — for every caller, so no two
checks can disagree about what "green" means.

Where `cmdread` gives up by finding no runner, this gives up by saying *masked*: when we
can no longer tell whether the shell overwrote the status, the honest answer is that the
exit code isn't the runner's.
"""

from __future__ import annotations

import shlex

from .. import runlog
from ...model import UNSTRUCTURED_RESULT, Session
from .cmdread import (
    _MAX_CMD_LEN,
    _covers,
    _MAX_UNWRAP_DEPTH,
    _SEGMENT_TOKENS,
    _SHELL_TOOLS,
    _exec_argv,
    _is_runner,
    _normalize_segment,
    _runner_segment,
    _unwrap,
)

# pytest's verdict is the last line; the slack absorbs the trailing warnings block. Small on
# purpose — further up we'd be reading the run, not its conclusion.
_SUMMARY_TAIL_LINES = 12


# Slack between the harness's event clock and ours. Generous on purpose — the ambiguity rule
# below is what protects correctness, not this number.
_EXEC_CLOCK_SLACK = 5.0


def _exec_run_for(event, commands) -> "CommandRun | None":  # noqa: F821 — model.CommandRun
    """The `tycho exec` evidence for this transcript event, or None. Matched on the inner argv,
    and **an ambiguous match is no match**.

    `commands.jsonl` is shared by every process in the repo, so `tycho exec -- pytest -q`
    appears twice when two agents work one tree. Picking the newest let agent B's pass answer
    for agent A's failure — VERIFIED on a red suite, citing the strongest evidence there is.

    ponytail: argv + a time window, no process identity. `tycho exec` could stamp its pid if
    the window proves too coarse.
    """
    if not commands:
        return None
    argv = _exec_argv(event.input.get("command") or "")
    if not argv:
        return None
    # The event is stamped when the tool *finished*; a run that began after that is a
    # different run.
    latest = (event.ts or 0.0) + _EXEC_CLOCK_SLACK
    matches = []
    for run in commands:
        try:
            if shlex.split(run.cmd) != argv:
                continue
        except ValueError:
            continue
        if event.ts and run.started_at > latest:
            continue  # a later run, by us or by another agent in this repo
        matches.append(run)
    return matches[-1] if len(matches) == 1 else None


def _status_is_masked(cmd: str, _depth: int = 0) -> bool:
    """True when the exit status the harness recorded is *not* the runner's own.

        pytest | tail -1      the pipeline's status is tail's (no pipefail here)
        pytest; echo done     `;` discards what came before
        pytest || true        the failure is swallowed by construction

    `&&` is safe and must NOT be flagged. A wrapper is masked when its inner command is.

    When in doubt say masked and let `_outcome` fall back to the runner's own output — that
    includes giving up past the bounds, where a masking operator could hide beyond where we
    stopped looking. The opposite give-up direction from `_runner_segment`.
    """
    if len(cmd) > _MAX_CMD_LEN or _depth > _MAX_UNWRAP_DEPTH:
        return True
    parts = _SEGMENT_TOKENS.split(cmd)  # [segment, sep, segment, sep, ..., segment]
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not _is_runner(_normalize_segment(seg)):
            inner = _unwrap(seg)
            if inner is None or _runner_segment(inner) is None:
                continue  # not the runner segment, wrapped or otherwise
            if _status_is_masked(inner, _depth + 1):
                return True  # a masking operator inside the wrapper hides the status
        # The first separator that redirects, swallows, or supersedes the status masks it.
        for j in range(i + 1, len(parts), 2):
            sep = parts[j]
            rest = parts[j + 1] if j + 1 < len(parts) else ""
            if sep in ("|", "||"):
                return True  # status replaced downstream, or the failure swallowed
            if sep in (";", "\n") and rest.strip():
                return True  # something ran after; its status is what got recorded
        return False  # nothing after it can overwrite the status — trust the exit code
    return False  # no runner in this command at all


def _captured_output(event) -> str:
    """The runner's own words, tail-first — or "" when the harness kept none.

    Tail only: Claude Code caps `toolUseResult.stdout` at 30k and keeps the *head* (checked
    against 2356 real payloads) while pytest prints its summary last, so a truncated capture
    reports nothing rather than matching a stray "5 passed" from a red run.
    """
    result = event.result or {}
    text = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr")).strip()
    return "\n".join(text.splitlines()[-_SUMMARY_TAIL_LINES:]) if text else ""


def _was_refused(event) -> bool:
    """True when the harness never let this command reach the shell.

    A command that ran comes back structured, with stdout/stderr, whatever its exit code. A
    refusal — unapproved permission rule, denied tool — comes back as prose with `is_error`
    set and nothing captured. Read as a failure it is a false red: a real Sonnet session that
    could not get `python3 -m unittest` approved retried six times and Tycho reported
    "`python3 -m unittest discover -s tests -v` ran but reported an error", FAILED, about a
    suite that never executed. Crying wolf costs more trust than staying quiet, so an
    unapproved command is "can't tell", never "failed".
    """
    return bool(event.is_error) and UNSTRUCTURED_RESULT in (event.result or {})


def _outcome(event, commands=()) -> bool | None:
    """Did this runner invocation fail? True = failed, False = passed, None = can't tell.

    One predicate for every caller, so no two checks disagree about what "green" means.
    Evidence ladder, strongest first: a status Tycho captured itself (`tycho exec` read
    `wait()`), the transcript's exit code when nothing masked it, then the runner's own summary
    line. When the first two disagree, failure wins — `tycho exec -- pytest && ./deploy.sh` can
    fail for a reason the capture can't see.
    """
    if _was_refused(event):
        return None
    run = _exec_run_for(event, commands)
    if run is not None:
        masked = _status_is_masked(event.input.get("command") or "")
        return run.failed or bool(event.is_error and not masked)
    if not _status_is_masked(event.input.get("command") or "") and event.is_error is not None:
        return event.is_error
    return runlog.outcome(_captured_output(event))


def _runner_events(events) -> list:
    """The test/build runner invocations among ``events`` — pass the scope you mean."""
    return [
        e
        for e in events
        if e.tool in _SHELL_TOOLS and _runner_segment(e.input.get("command") or "") is not None
    ]


def _unresolved_reds(events, commands) -> list:
    """Failed runner invocations that no later green run covers.

    Run the suite, see red, narrow to the failing file, go green, stop — the standard agent
    loop, and taking that last success at face value reported VERIFIED. So a green supersedes a
    red only when it ran *at least as much*: the same command, or the whole suite.

    Identity alone was its own bug — a re-run with anything but byte-identical argv left the
    red unresolved, pinning the `test_*` checks adverse with no way to discharge them.
    """
    runs = sorted(_runner_events(events), key=lambda e: e.ts)
    reds = []
    for e in runs:
        if _outcome(e, commands) is not True:
            continue
        red_cmd = _runner_segment(e.input.get("command") or "")
        if any(
            later.ts > e.ts
            and _outcome(later, commands) is False
            and _covers(_runner_segment(later.input.get("command") or ""), red_cmd)
            for later in runs
        ):
            continue
        reds.append(e)
    return reds


def _last_green_run_ts(session: Session) -> float | None:
    # Session-scoped: a run three turns back still covers a source unchanged since. A green
    # following an unresolved red is the narrowed re-run, not the suite.
    reds = _unresolved_reds(session.events, session.commands)
    greens = [
        e.ts
        for e in _runner_events(session.events)
        if _outcome(e, session.commands) is False and not any(r.ts < e.ts for r in reds)
    ]
    return max(greens) if greens else None

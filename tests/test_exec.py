"""`tycho exec` — real command evidence (strategy §9.6).

Three properties, and they are in tension, which is why they all get tests:

1. **It must not change the command.** Same exit code, same live output, same interactivity:
   the child inherits Tycho's stdio, so it keeps its TTY. `tycho exec` is meant to be safe to
   prefix onto anything.
2. **It must record what the command returned** — the exit status, including for a command
   that doesn't exist.
3. **The evidence must reach the checks, and must not be able to vouch for the wrong turn.**

The eval (`tests/test_eval.py`) scores what this buys; this file proves the mechanism.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tycho import checks, command
from tycho import verify as engine
from tycho.model import CommandRun, Event, Session
from tycho.config import Config

PY = sys.executable
WINDOWS = os.name == "nt"


def _entries(repo: Path) -> list[dict]:
    """Every raw line of the evidence log, as written."""
    path = command.path_for(repo)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(repo: Path, *code: str) -> int:
    """`tycho exec -- python -c <code>` against `repo`."""
    return command.execute(repo, ["--", PY, "-c", "\n".join(code)])


# --- 1. it must not change the command --------------------------------------


def test_output_streams_live_rather_than_buffering_until_exit(tmp_path: Path):
    """The bug the placeholder had: `capture_output=True` shows the user nothing until the
    command finishes, which for a test run means staring at a blank terminal.

    Asserted the only way it can honestly be asserted — from outside, in real time. A child
    prints, then sleeps; if we can read that line off `tycho exec`'s stdout while the child
    is still alive, the tee is real. An in-process assertion on the captured buffer would
    pass just as well against the buffering version.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out.txt"
    child = "import sys, time; print('STREAMED', flush=True); time.sleep(3)"
    with out.open("wb") as fh:
        proc = subprocess.Popen(
            [PY, "-m", "tycho", "exec", "--", PY, "-c", child],
            cwd=repo, stdout=fh, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 2.5
            while time.time() < deadline:
                if b"STREAMED" in out.read_bytes():
                    break
                time.sleep(0.05)
            saw_it_early = b"STREAMED" in out.read_bytes()
            still_running = proc.poll() is None
        finally:
            proc.kill()
            proc.wait()
    assert saw_it_early, "output only appeared after the command exited — the tee is buffering"
    assert still_running, "the child exited too fast to prove anything; lengthen the sleep"


def test_exit_code_zero_is_forwarded(tmp_path: Path):
    assert _run(tmp_path, "print('ok')") == 0


@pytest.mark.parametrize("code", [1, 2, 42, 127])
def test_nonzero_exit_codes_are_forwarded_unchanged(tmp_path: Path, code: int):
    """Unchanged, always — this is what makes `tycho exec` safe to prefix onto anything.
    127 is in the list on purpose: our own not-found code must not be special-cased."""
    assert _run(tmp_path, f"import sys; sys.exit({code})") == code
    assert _entries(tmp_path)[-1]["exit"] == code


@pytest.mark.skipif(WINDOWS, reason="POSIX signal semantics; Windows has no SIGTERM exit convention")
def test_signal_death_reports_the_conventional_128_plus_signal(tmp_path: Path):
    """A signal-killed child is what the shell calls 128+N. Python's `Popen.wait` says -N,
    and passing that through would surface as 241 (256-15) — a number nothing in a shell
    script or CI gate knows how to read."""
    assert _run(tmp_path, f"import os, signal; os.kill(os.getpid(), {signal.SIGTERM})") == 143
    assert _entries(tmp_path)[-1]["exit"] == 143


def test_stderr_stays_on_stderr(tmp_path: Path, capfd):
    """Merging the streams would be simpler and would break `tycho exec -- x 2>log`."""
    _run(tmp_path, "import sys; print('OUT'); print('ERR', file=sys.stderr)")
    captured = capfd.readouterr()
    assert "OUT" in captured.out and "OUT" not in captured.err
    assert "ERR" in captured.err and "ERR" not in captured.out


def test_empty_command_is_a_usage_error_and_logs_nothing(tmp_path: Path):
    assert command.execute(tmp_path, []) == 2
    assert command.execute(tmp_path, ["--"]) == 2
    assert _entries(tmp_path) == []


def test_command_not_found_returns_127_and_is_itself_evidence(tmp_path: Path):
    """"The command the agent said it ran does not exist" is a fact no transcript records,
    so it gets logged rather than dropped."""
    assert command.execute(tmp_path, ["--", "definitely-not-a-real-binary-xyz"]) == 127
    entry = _entries(tmp_path)[-1]
    assert entry["exit"] == 127
    assert "definitely-not-a-real-binary-xyz" in entry["cmd"]


# --- 2. capturing the part that carries the verdict -------------------------


def test_the_entry_carries_command_status_and_timings(tmp_path: Path):
    before = time.time()
    _run(tmp_path, "print('hi')")
    entry = _entries(tmp_path)[-1]
    assert entry["schema"] == command.SCHEMA
    assert entry["exit"] == 0
    assert before <= entry["started_at"] <= entry["ended_at"] <= time.time()


def test_secrets_are_redacted_from_the_command(tmp_path: Path):
    """One redaction policy, `record.redact` — reused, not re-implemented, so the evidence
    log can't quietly become the one durable file that keeps credentials."""
    command.execute(tmp_path, ["--", PY, "-c", "print('hi')",
                               "--token", "ghp_abcdefghijklmnopqrstuvwxyz012345"])
    entry = _entries(tmp_path)[-1]
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in entry["cmd"]
    assert "[REDACTED]" in entry["cmd"]


def test_the_log_is_capped_so_it_cannot_grow_unboundedly(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TYCHO_COMMANDS_MAX", "5")
    monkeypatch.setattr(command, "_PRUNE_SLACK", 2)
    path = command.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        command._log(tmp_path, ["echo", str(i)], 0, 1000.0 + i, 1000.0 + i)
    entries = _entries(tmp_path)
    assert len(entries) <= 5 + 2
    # Newest kept, oldest dropped — an evidence log that prunes the recent end is useless.
    assert entries[-1]["cmd"].endswith("19")


def test_a_log_that_cannot_be_written_never_breaks_the_command(tmp_path: Path):
    """The Stop-hook rule, applied here: evidence is never worth failing a run over. The
    `.tycho` path is occupied by a *file*, so every write below it fails."""
    (tmp_path / ".tycho").write_text("not a directory")
    assert _run(tmp_path, "print('still ran')") == 0


def test_a_corrupt_or_foreign_log_line_costs_only_that_line(tmp_path: Path):
    path = command.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"schema": 1, "cmd": "pytest -q", "exit": 1, "started_at": 50.0,
            "ended_at": 51.0}
    path.write_text(
        "\n".join([
            "{not json at all",
            json.dumps({"schema": 99, "cmd": "future", "exit": 0, "started_at": 50.0}),
            json.dumps(good),
            '{"schema": 1, "cmd": "trunc',  # a killed process's half-written final line
        ]),
        encoding="utf-8",
    )
    runs = command.read(tmp_path, since=1.0)
    assert [r.cmd for r in runs] == ["pytest -q"]


def test_read_declines_without_a_time_anchor(tmp_path: Path):
    """`since=0.0` means "we have no idea when this turn was". The safe answer to that is
    no evidence, not all of it — see `verify._evidence_floor`."""
    command._log(tmp_path, ["pytest", "-q"], 0, 100.0, 101.0)
    assert command.read(tmp_path, since=0.0) == ()
    assert command.read(tmp_path, since=100.0)


# --- 3. the evidence reaching the checks ------------------------------------


def _event(cmd: str, ts: float, is_error: bool | None = None, result: dict | None = None) -> Event:
    return Event(ts=ts, tool="Bash", input={"command": cmd}, is_error=is_error, result=result or {})


def _session(events, commands=(), turn_start: float = 0.0) -> Session:
    return Session(
        events=tuple(events), edits=(), repo=Path("/repo"), config=Config(),
        commands=tuple(commands), turn_start=turn_start,
    )


def _ran(cmd: str, exit_code: int, ts: float = 100.0) -> CommandRun:
    return CommandRun(cmd=cmd, exit_code=exit_code, started_at=ts, ended_at=ts + 1)


def test_a_runner_behind_tycho_exec_is_still_recognized_as_a_runner():
    """Without this, prefixing `tycho exec` would *hide* the test run — the opposite of
    the point. `_unwrap` already did it for `tycho run`; both hatches behave alike."""
    assert checks._runner_segment("tycho exec -- make test") == "make test"
    assert checks._runner_segment("tycho exec pytest -q | tail -1") == "pytest -q"
    assert checks._runner_segment("bash -c 'tycho exec -- pytest'") == "pytest"


def test_exec_argv_finds_the_inner_command_through_wrappers():
    assert checks._exec_argv("tycho exec -- pytest -q") == ["pytest", "-q"]
    assert checks._exec_argv("tycho exec pytest -q; echo done") == ["pytest", "-q"]
    assert checks._exec_argv("bash -c 'tycho exec -- pytest -q'") == ["pytest", "-q"]
    assert checks._exec_argv("tycho run -- pytest -q") is None  # `run` captures nothing
    assert checks._exec_argv("pytest -q") is None


def test_oversized_command_gives_up_in_the_safe_direction_per_function():
    """Both scanners must bail once a command is absurdly large — but in opposite directions.
    `_runner_segment`/`_exec_argv` giving up as None just loses evidence of a test run, which
    degrades a check to UNSUPPORTED. `_status_is_masked` giving up as False would instead trust
    a status it never actually checked — the fabricated green this program exists to prevent —
    so it must give up as True (masked) instead."""
    huge = "pytest -k '" + ("a" * (checks._MAX_CMD_LEN + 1)) + "'"
    assert len(huge) > checks._MAX_CMD_LEN
    assert checks._runner_segment(huge) is None
    assert checks._exec_argv(huge) is None
    assert checks._status_is_masked(huge) is True


def test_deeply_nested_wrappers_give_up_instead_of_hanging():
    """A wrapper nested past `_MAX_UNWRAP_DEPTH` — `bash -c "bash -c \\"...\\""`, or a long
    chain of non-quoting wrappers like `env -- env -- ...` — must not blow the recursion limit
    or take more than a beat. Same direction split as the size bound above."""
    nested = "pytest"
    for _ in range(checks._MAX_UNWRAP_DEPTH + 5):
        nested = "bash -c " + repr(nested)
    assert checks._runner_segment(nested) is None
    assert checks._status_is_masked(nested) is True

    chained = "pytest -q"
    for _ in range(5000):
        chained = "env -- " + chained
    assert len(chained) <= checks._MAX_CMD_LEN * 20  # sanity: this shape stays linear-size
    assert checks._runner_segment(chained) is None
    assert checks._status_is_masked(chained) is True


def test_a_wrapper_within_bounds_still_unwraps_normally():
    """The bound must not clip real invocations — a couple of wrapper layers is routine."""
    assert checks._runner_segment("bash -c 'timeout 30 pytest -q'") is not None
    assert checks._status_is_masked("pytest -q | tail -1") is True
    assert checks._status_is_masked("pytest -q && echo ok") is False


def test_exec_evidence_beats_a_masked_transcript_status():
    """The headline case. The shell reported the pipeline's 0; Tycho watched pytest exit 1."""
    session = _session(
        [_event("tycho exec -- pytest -q | tail -1", 100.0, is_error=False)],
        [_ran("pytest -q", 1, ts=99.0)],
    )
    result = checks.command_execution(session)
    assert result.status is checks.CheckStatus.FAIL
    assert "exit 1" in result.evidence


def test_exec_evidence_answers_where_the_transcript_recorded_nothing_at_all():
    """The `runner_exit_status_not_recorded` shape: no status, no output, no result."""
    session = _session([_event("tycho exec -- pytest -q", 100.0, is_error=None)],
                       [_ran("pytest -q", 1, ts=99.0)])
    assert checks.command_execution(session).status is checks.CheckStatus.FAIL


def test_exec_evidence_also_carries_the_greens():
    """An evidence channel that only ever produces failures is a pessimist, not a verifier."""
    session = _session([_event("tycho exec -- pytest -q", 100.0, is_error=None)],
                       [_ran("pytest -q", 0, ts=99.0)])
    result = checks.command_execution(session)
    assert result.status is checks.CheckStatus.PASS
    assert "exit 0" in result.evidence


def test_an_unmasked_transcript_failure_can_add_a_failure_but_never_remove_one():
    """The asymmetry in `checks._outcome`. `tycho exec -- pytest && ./deploy.sh` can fail for
    a reason Tycho's capture cannot see; calling that green because pytest passed would be a
    fabricated green. The reverse — a masked red over a captured green — is not a failure at
    all, it's someone post-processing their own output."""
    both_ran = _event("tycho exec -- pytest -q && ./deploy.sh", 100.0, is_error=True)
    assert checks._outcome(both_ran, (_ran("pytest -q", 0, ts=99.0),)) is True

    masked = _event("tycho exec -- pytest -q | grep -c FAILED", 100.0, is_error=True)
    assert checks._outcome(masked, (_ran("pytest -q", 0, ts=99.0),)) is False


def test_two_agents_running_the_same_command_is_not_evidence_for_either():
    """`commands.jsonl` is repo-scoped and shared by every process in the repo.

    Two agents both run `pytest -q`; A's fails, B's passes a second later. Crediting the
    newest match let B's green answer for A's red turn — VERIFIED on a failing suite,
    citing "Tycho ran it" as the authority. That is the fabricated green this program
    exists to prevent, so an ambiguous match must resolve to "cannot tell".
    """
    event = _event("tycho exec -- pytest -q", 100.0, is_error=None)
    runs = (_ran("pytest -q", 1, ts=98.0), _ran("pytest -q", 0, ts=99.0))
    assert checks._outcome(event, runs) is None
    assert checks._outcome(event, tuple(reversed(runs))) is None


def test_a_run_that_started_after_the_event_finished_is_a_different_run():
    """The harness stamps the event when the tool finished; a later run isn't the one it
    recorded, so it can neither vindicate nor condemn it."""
    event = _event("tycho exec -- pytest -q", 100.0, is_error=None)
    assert checks._outcome(event, (_ran("pytest -q", 0, ts=200.0),)) is None
    assert checks._outcome(event, (_ran("pytest -q", 1, ts=99.0),)) is True  # in-window, used


def test_evidence_for_a_different_command_is_not_credited():
    session = _session([_event("tycho exec -- pytest -q", 100.0, is_error=None)],
                       [_ran("ruff check", 1, ts=99.0)])
    # No match, so nothing is known — never "it failed" and never "it passed".
    assert checks._outcome(session.events[0], session.commands) is None
    assert checks.command_execution(session).status is checks.CheckStatus.UNSUPPORTED


def test_a_plain_run_with_no_exec_evidence_behaves_exactly_as_before():
    """The regression guard for everyone who never types `tycho exec`."""
    for cmd, is_error in (("pytest -q", False), ("pytest -q", True)):
        event = _event(cmd, 100.0, is_error=is_error)
        assert checks._outcome(event) is is_error
        assert checks._outcome(event, (_ran("something-else", 1),)) is is_error


# --- staleness ---------------------------------------------------------------


def test_gather_floors_the_evidence_at_the_turn_boundary(tmp_path: Path):
    """An exec run from an *earlier* turn must not vouch for this one. The floor lives in
    `gather` — the I/O boundary — so no check has to remember to apply it."""
    command._log(tmp_path, ["pytest", "-q"], 1, 100.0, 101.0)   # last turn
    command._log(tmp_path, ["pytest", "-q"], 0, 200.0, 201.0)   # this turn
    assert [r.exit_code for r in command.read(tmp_path, since=150.0)] == [0]
    assert engine._evidence_floor((), (), 150.0) == 150.0


def test_the_floor_falls_back_to_the_start_of_the_session_then_to_nothing():
    events = (_event("pytest -q", 500.0), _event("ruff check", 400.0))
    assert engine._evidence_floor(events, (), 0.0) == 400.0
    # No turn boundary and no timestamps anywhere: admit nothing rather than everything.
    assert engine._evidence_floor((), (), 0.0) == 0.0


def test_a_stale_exec_run_cannot_be_credited_to_a_later_turn(tmp_path: Path, monkeypatch):
    """End to end through `gather`: yesterday's green `pytest` in the log, today's turn
    claiming a green run. The floor is what stops that from becoming a fabricated green."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-13T14:20:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "tycho exec -- pytest -q | tail -1"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    turn_start = engine.events_mod.parse(transcript)[0].ts
    command._log(tmp_path, ["pytest", "-q"], 0, turn_start - 86_400, turn_start - 86_399)

    session = engine.gather(transcript, tmp_path, turn_start=lambda _p: turn_start)
    assert session.commands == (), "an exec run from a previous day was offered as evidence"
    assert checks.command_execution(session).status is checks.CheckStatus.UNSUPPORTED

    # The same run, inside the turn, is evidence.
    command._log(tmp_path, ["pytest", "-q"], 1, turn_start + 1, turn_start + 2)
    session = engine.gather(transcript, tmp_path, turn_start=lambda _p: turn_start)
    assert [r.exit_code for r in session.commands] == [1]
    assert checks.command_execution(session).status is checks.CheckStatus.FAIL


def test_gather_survives_a_missing_or_unreadable_evidence_log(tmp_path: Path):
    """Nothing in the Stop-hook path may raise, and `gather` is squarely in it."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert engine.gather(transcript, tmp_path).commands == ()
    (tmp_path / ".tycho").mkdir()
    (command.path_for(tmp_path)).mkdir()  # a directory where the log should be
    assert engine.gather(transcript, tmp_path, turn_start=lambda _p: 1.0).commands == ()


def test_the_turn_record_reports_what_tycho_observed(tmp_path: Path):
    """The receipt and the verdict must read the same evidence, or `tycho show` will say
    "passed" next to a FAILED verdict for the same command."""
    from tycho import record

    session = _session([_event("tycho exec -- pytest -q | tail -1", 100.0, is_error=False)],
                       [_ran("pytest -q", 1, ts=99.0)], turn_start=50.0)
    built = record.build(session, [], "FAILED", "claude", ended_at=200.0)
    assert built["commands"][0]["outcome"] == "failed"


# --- Windows launchability ----------------------------------------------------
#
# npm/yarn/pnpm/npx/gradlew/mvnw ship as .cmd shims, and Windows CreateProcess runs only PE
# images — so these are exactly the runners `checks.py` recognizes and exactly the ones a
# bare Popen cannot start. Getting it wrong turns a passing build into exit 127 from a
# command whose whole contract is forwarding the child's status unchanged.


def test_launchable_is_a_passthrough_off_windows(monkeypatch):
    monkeypatch.setattr(command.os, "name", "posix")
    assert command.launchable(["npm", "test"]) == ["npm", "test"]


def test_launchable_routes_a_cmd_shim_through_the_interpreter(monkeypatch):
    monkeypatch.setattr(command.os, "name", "nt")
    monkeypatch.setattr(command.shutil, "which", lambda c: r"C:\node\npm.cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\cmd.exe")
    assert command.launchable(["npm", "test"]) == [
        r"C:\Windows\cmd.exe", "/c", r"C:\node\npm.cmd", "test",
    ]


def test_launchable_leaves_a_real_executable_alone(monkeypatch):
    monkeypatch.setattr(command.os, "name", "nt")
    monkeypatch.setattr(command.shutil, "which", lambda c: r"C:\Python\python.exe")
    assert command.launchable(["python", "-m", "pytest"]) == ["python", "-m", "pytest"]


def test_launchable_survives_an_unresolvable_command(monkeypatch):
    """`which` returns None for a command that isn't there; Popen must still get the
    original argv so the error the user sees names what they actually typed."""
    monkeypatch.setattr(command.os, "name", "nt")
    monkeypatch.setattr(command.shutil, "which", lambda c: None)
    assert command.launchable(["nope"]) == ["nope"]
    assert command.launchable([]) == []

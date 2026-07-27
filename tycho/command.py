"""`tycho exec` — put a command's real exit status on the record (strategy §9.6).

The pitch is *"your commands and what they returned are on the record, so the receipt is
real."*

It is also what makes the dormant harnesses worth enabling: 3 of the 4 known misses in
`tests/test_eval.py` are structural, because the harness recorded no stdout or no exit
status. That gap never closes as models improve — it is a property of the harness, not of
the model — so the only fix is to observe the run ourselves. Nothing here reads the
harness; the evidence is identical for Claude Code, Codex, Cursor and OpenCode.

The child inherits Tycho's stdio, so it owns the terminal: a TTY, its colours, byte-for-byte
streaming, and the ability to prompt. Tycho is only the parent process reading `wait()`.
`tycho run` (cli.py:_run) is the same shape without the evidence line. Both are recognized
by ``checks._unwrap``, so a runner hidden behind either is still visible as a runner.

**Never alters the exit code the caller sees**, so it is safe to prefix onto anything. A
signal-killed child reports the conventional 128+signal, which is what the shell would
have reported had Tycho not been in the way.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import state
from .model import CommandRun

SCHEMA = 1
FILE = "commands.jsonl"

_MAX_CMD_CHARS = 500

_MAX_DEFAULT = 500
_PRUNE_SLACK = 100

# Shell convention. `command not found` is 127 everywhere, and returning it means a caller's
# `tycho exec -- typo && deploy` short-circuits exactly as it would without us in the way.
# A missing *command* is the child's failure, not a usage error — an empty argv is ours (2).
_NOT_FOUND = 127


def max_records() -> int:
    """How many command runs `commands.jsonl` keeps. Default 500; `TYCHO_COMMANDS_MAX` overrides."""
    from . import record

    return record._env_cap("TYCHO_COMMANDS_MAX", _MAX_DEFAULT)


def path_for(repo: Path) -> Path:
    """`<repo>/.tycho/commands.jsonl` — the command evidence log."""
    return state.dir_for(repo) / FILE


# --- running -----------------------------------------------------------------


def execute(repo: Path, argv: list[str]) -> int:
    """Run `argv` with our own stdio and log the evidence.

    Returns the child's exit code **unchanged** (128+signal for a signal-killed child).
    Nothing in the evidence path can change that, and nothing here raises: a log we can't
    write is simply not written.
    """
    cmd = argv[1:] if argv and argv[0] == "--" else argv
    if not cmd:
        print("tycho exec: give a command, e.g. tycho exec -- pytest -q", file=sys.stderr)
        return 2

    started = time.time()
    try:
        proc = subprocess.Popen(cmd)
    except OSError as exc:  # not found, not executable, not a directory — all the same here
        print(f"tycho exec: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        # Still evidence, and the most useful kind: "the command the agent claimed to run
        # does not exist" is a fact a transcript will never tell you.
        _log(repo, cmd, _NOT_FOUND, started, time.time())
        return _NOT_FOUND

    rc = _wait(proc)
    code = 128 - rc if rc < 0 else rc  # POSIX reports -N for signal N; the shell says 128+N
    _log(repo, cmd, code, started, time.time())
    return code


def _wait(proc: subprocess.Popen) -> int:
    """Wait for the child, surviving a Ctrl-C.

    On both POSIX and Windows the interrupt reaches the whole console/process group, so the
    child is already dying — we wait for it and report *its* status rather than inventing
    one. A second Ctrl-C means the user wants out now, so we kill and take what we get.
    """
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            try:
                return proc.wait(timeout=5)
            except KeyboardInterrupt:
                proc.kill()
            except subprocess.TimeoutExpired:
                proc.kill()


# --- the evidence log --------------------------------------------------------


def _log(repo: Path, cmd: list[str], code: int, started: float, ended: float) -> None:
    """Append one evidence line. Never raises — evidence is never worth failing a run over."""
    # Imported here, not at module scope: `verify` imports this module and `record` imports
    # `verify`, so a top-level import would close a cycle. Nothing needs it before call time.
    from . import record

    try:
        entry = {
            "schema": SCHEMA,
            # Same redaction table as the turn record — one policy, not two.
            "cmd": record._clean(shlex.join(cmd), _MAX_CMD_CHARS),
            "exit": int(code),
            "started_at": started,
            "ended_at": ended,
        }
        path = path_for(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        # Slack differs from the turn record's: commands are logged far more often than turns.
        record._prune(path, max_records(), _PRUNE_SLACK)
    except Exception:
        return


def read(repo: Path, since: float = 0.0) -> tuple[CommandRun, ...]:
    """Every logged run that *started at or after* `since`, oldest first. Never raises.

    `since` is the staleness guard and it is not optional: this log outlives any one turn,
    session or harness, so an unbounded read would happily offer yesterday's green `pytest`
    as evidence for today's claim — a fabricated green, the one thing Tycho must never do.
    `verify.gather` supplies the floor; **`since=0.0` yields nothing**, because "no time
    anchor at all" is a reason to admit no evidence, not a reason to admit all of it.
    """
    from . import record

    if since <= 0.0:
        return ()
    runs = (_parse(row) for row in record.iter_jsonl(path_for(repo)))
    return tuple(r for r in runs if r is not None and r.started_at >= since)


def _parse(row: dict) -> CommandRun | None:
    """One log row → a CommandRun, or None for a corrupt/foreign-schema one.

    Skipping rather than raising: a half-written final line from a killed process must cost
    us that line, never the whole read.
    """
    if row.get("schema") != SCHEMA:
        return None
    try:
        return CommandRun(
            cmd=str(row["cmd"]),
            exit_code=int(row["exit"]),
            started_at=float(row["started_at"]),
            ended_at=float(row.get("ended_at") or row["started_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None

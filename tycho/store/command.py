"""`tycho exec` — put a command's real exit status on the record.

Observing the run ourselves is the only fix for harnesses that record no stdout or exit
status. Nothing here reads the harness; the evidence is identical for every one.

The child inherits Tycho's stdio, so it owns the terminal (TTY, colours, streaming,
prompting). `tycho run` (cli.py:_run) is the same shape without the evidence line; both are
recognized by ``checks._unwrap``.

**Never alters the exit code the caller sees**, so it is safe to prefix onto anything; a
signal-killed child reports the conventional 128+signal.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
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

# Shell convention: `command not found` is 127, so `tycho exec -- typo && deploy`
# short-circuits as it would without us. An empty argv is our usage error (2).
_NOT_FOUND = 127


def max_records() -> int:
    """How many runs `commands.jsonl` keeps. Default 500; `TYCHO_COMMANDS_MAX` overrides."""
    from . import record

    return record._env_cap("TYCHO_COMMANDS_MAX", _MAX_DEFAULT)


def path_for(repo: Path) -> Path:
    """`<repo>/.tycho/commands.jsonl` — the command evidence log."""
    return state.dir_for(repo) / FILE


# --- running -----------------------------------------------------------------


def execute(repo: Path, argv: list[str]) -> int:
    """Run `argv` with our own stdio and log the evidence.

    Returns the child's exit code **unchanged**; a log we can't write is simply not written.
    """
    cmd = argv[1:] if argv and argv[0] == "--" else argv
    if not cmd:
        print("tycho exec: give a command, e.g. tycho exec -- pytest -q", file=sys.stderr)
        return 2

    started = time.time()
    try:
        proc = subprocess.Popen(launchable(cmd))
    except OSError as exc:  # not found, not executable, not a directory — all the same here
        print(f"tycho exec: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        # Still evidence: "the claimed command does not exist" is not in any transcript.
        _log(repo, cmd, _NOT_FOUND, started, time.time())
        return _NOT_FOUND

    rc = _wait(proc)
    code = 128 - rc if rc < 0 else rc  # POSIX reports -N for signal N; the shell says 128+N
    _log(repo, cmd, code, started, time.time())
    return code


def launchable(cmd: list[str]) -> list[str]:
    """`cmd`, made runnable by CreateProcess on Windows. Unchanged everywhere else.

    Windows can only execute PE images, but `npm`, `yarn`, `pnpm`, `npx`, `gradlew` and
    `mvnw` all ship as `.cmd`/`.bat` shims — and those are exactly the runners `checks.py`
    recognizes, so this is the common case, not a corner. Without the `cmd.exe /c` hop,
    `tycho exec -- npm test` returns 127 for a build that would have passed, which for a
    command that promises to forward the child's status unchanged is the worst possible
    failure: Tycho turning a green run red.

    `shutil.which` is what honours PATHEXT and resolves `npm` to `npm.cmd`.
    """
    if os.name != "nt" or not cmd:
        return cmd
    exe = shutil.which(cmd[0])
    if exe and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *cmd[1:]]
    return cmd


def _wait(proc: subprocess.Popen) -> int:
    """Wait for the child, surviving a Ctrl-C.

    The interrupt reaches the whole process group, so the child is already dying — report
    *its* status rather than inventing one. A second Ctrl-C kills and takes what we get.
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
    # Local import: `verify` imports this module and `record` imports `verify` — a top-level
    # import would close the cycle.
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
        state._private_dir(path.parent)
        line = json.dumps(entry, ensure_ascii=True, separators=(",", ":"))
        state._touch_private(path)
        # Same durability policy as the turn record: 0600, a repaired final newline, and the
        # prune only under the lock it needs.
        with state._locked(path) as held:
            record._terminate(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if held:
                # Slack differs from the turn record's: commands are logged far more often.
                record._prune(path, max_records(), _PRUNE_SLACK)
    except Exception:
        return


def read(repo: Path, since: float = 0.0) -> tuple[CommandRun, ...]:
    """Every logged run that *started at or after* `since`, oldest first. Never raises.

    `since` is the staleness guard: this log outlives any turn, so an unbounded read would
    offer yesterday's green `pytest` as evidence for today's claim. **`since=0.0` yields
    nothing** — no time anchor means admit no evidence, not admit all of it.
    """
    from . import record

    if since <= 0.0:
        return ()
    runs = (_parse(row) for row in record.iter_jsonl(path_for(repo)))
    return tuple(r for r in runs if r is not None and r.started_at >= since)


def _parse(row: dict) -> CommandRun | None:
    """One log row → a CommandRun, or None for a corrupt/foreign-schema one.

    Skipped rather than raised: a half-written final line costs that line, not the read.
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

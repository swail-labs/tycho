"""`tycho exec` — put a command's real output and exit status on the record (strategy §9.6).

Not "it closes our blind spots" — that is our problem, not the user's. The pitch is *"your
commands and their output are on the record, so the receipt is real."*

It is also what makes the dormant harnesses worth enabling: 3 of the 4 known misses in
`tests/test_eval.py` are structural, because the harness recorded no stdout or no exit
status. That gap never closes as models improve — it is a property of the harness, not of
the model — so the only fix is to capture the run ourselves. Nothing here reads the
harness; the evidence is identical for Claude Code, Codex, Cursor and OpenCode.

**`run` vs `exec`, and why both survive.** They are not two spellings of one thing:

- ``tycho run`` (cli.py:_run) hands the child *our own* stdio. The child owns the terminal:
  it sees a TTY, keeps its colours, streams byte-for-byte, and can prompt for input. It
  captures nothing; its whole job is to forward a truthful exit code into the transcript.
- ``tycho exec`` puts the child behind a pipe so Tycho can keep the bytes. Live output is
  preserved (we tee every chunk straight through, unbuffered), but a pipe is not a TTY, so
  a colour-detecting runner goes monochrome and a progress bar that rewrites its line will
  look different. That is a real cost, and it is exactly why `run` is not deleted.

So: `exec` when you want the receipt, `run` when you want the child to own the terminal.
Merging them would mean forcing that pipe on every `tycho run` user — a documented
contract change for people who only ever wanted an unmasked exit code. Both are recognized
by ``checks._unwrap``, so a runner hidden behind either is still visible as a runner.

**Never alters the exit code the caller sees**, so it is safe to prefix onto anything. A
signal-killed child reports the conventional 128+signal, which is what the shell would
have reported had Tycho not been in the way.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import state
from .model import CommandRun

SCHEMA = 1
FILE = "commands.jsonl"

# How much of each stream survives into the evidence line, **tail-kept**. The head is the
# wrong half and that is not a guess: the third structural eval miss is precisely a harness
# that kept stdout's head while pytest printed its summary last, so the verdict was exactly
# what got cut (see the comment on `checks._captured_output`, verified against 2356 real
# payloads). When we own the capture we keep the end, because the end is where runners put
# their conclusion. 64 KiB is far more than any summary needs and small enough that a
# runaway build can't write a line no reader will load.
_MAX_CAPTURE_BYTES = 64 * 1024
# What actually lands on disk, after redaction (a JSON line, so this is the real bound).
_MAX_OUTPUT_CHARS = 8000
_MAX_CMD_CHARS = 500

_MAX_DEFAULT = 500
_PRUNE_SLACK = 100

# Shell convention. `command not found` is 127 everywhere, and returning it means a caller's
# `tycho exec -- typo && deploy` short-circuits exactly as it would without us in the way.
# A missing *command* is the child's failure, not a usage error — an empty argv is ours (2).
_NOT_FOUND = 127

# Read size. Paired with `bufsize=0` below, which is the whole streaming story: a buffered
# pipe's `read(n)` blocks until it has n bytes, which would hold a test runner's progress
# hostage until it produced 4 KiB. A raw FileIO returns whatever has arrived.
_CHUNK = 4096


def max_records() -> int:
    """How many command runs `commands.jsonl` keeps. Default 500; `TYCHO_COMMANDS_MAX` overrides.

    Same idiom and same fail-open rule as `record.max_records`: a junk value falls back to
    the default rather than raising, because this is read on a path that must never break a
    command the user is waiting on.
    """
    try:
        return max(1, int(os.environ.get("TYCHO_COMMANDS_MAX", _MAX_DEFAULT)))
    except (TypeError, ValueError):
        return _MAX_DEFAULT


def path_for(repo: Path) -> Path:
    """`<repo>/.tycho/commands.jsonl` — the command evidence log."""
    return state.dir_for(repo) / FILE


# --- running -----------------------------------------------------------------


def execute(repo: Path, argv: list[str]) -> int:
    """Run `argv`, stream its output live, capture it, and log the evidence.

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
        # bufsize=0 → raw pipes → short reads → live output. stdin is inherited, so a
        # command that prompts still works; only stdout/stderr are teed.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except OSError as exc:  # not found, not executable, not a directory — all the same here
        print(f"tycho exec: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        # Still evidence, and the most useful kind: "the command the agent claimed to run
        # does not exist" is a fact a transcript will never tell you.
        _log(repo, cmd, _NOT_FOUND, started, time.time(), "")
        return _NOT_FOUND

    out_buf, err_buf = bytearray(), bytearray()
    # Threads, not select/poll: `select` on pipes is POSIX-only and there is no `pty` on
    # Windows. Two blocking readers is the one shape that is identical on all three
    # platforms Tycho ships to. ponytail: interleaving between the two streams is
    # approximate in the capture — each stream still reaches the terminal in order, which
    # is what a human watching actually needs.
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, out_buf), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, err_buf), daemon=True),
    ]
    for t in pumps:
        t.start()

    rc = _wait(proc)
    for t in pumps:
        t.join(timeout=5)  # bounded: a wedged reader must not hang the log (or the caller)

    code = 128 - rc if rc < 0 else rc  # POSIX reports -N for signal N; the shell says 128+N
    _log(repo, cmd, code, started, time.time(), _decode(out_buf, err_buf))
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


def _pump(src, out, buf: bytearray) -> None:
    """Tee one stream: every chunk goes to the terminal immediately and into `buf`'s tail.

    Never raises — a broken terminal (closed stdout, a caller that walked away) must not
    take down the command the user is actually running.
    """
    try:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                return
            _write(out, chunk)
            buf += chunk
            # Trim to the tail, amortized: keep at most 2x the cap in memory, so a gigabyte
            # of build spam costs one copy per 64 KiB rather than per chunk.
            if len(buf) > 2 * _MAX_CAPTURE_BYTES:
                del buf[: len(buf) - _MAX_CAPTURE_BYTES]
    except Exception:
        return


def _write(stream, data: bytes) -> None:
    """Write raw bytes through `stream`, flushing so the user sees them *now*.

    Bytes, not text: a runner is free to emit non-UTF-8 (a filename in another encoding, a
    stray control byte) and re-encoding it would corrupt what the terminal shows. Falls
    back to a lossy decode only when the stream has no binary buffer — which is what a test
    harness's captured stdout looks like.
    """
    try:
        raw = getattr(stream, "buffer", None)
        if raw is not None:
            raw.write(data)
            raw.flush()
        else:
            stream.write(data.decode("utf-8", "replace"))
            stream.flush()
    except Exception:
        return


def _decode(out_buf: bytearray, err_buf: bytearray) -> str:
    """stdout then stderr, decoded leniently — the same order `checks._captured_output` joins.

    `errors="replace"` is load-bearing twice: a runner may emit non-UTF-8, and tail-trimming
    can slice a multi-byte character in half. Neither is worth losing the whole capture over.
    """
    return "\n".join(b.decode("utf-8", "replace") for b in (out_buf, err_buf) if b).strip()


# --- the evidence log --------------------------------------------------------


def _log(repo: Path, cmd: list[str], code: int, started: float, ended: float, output: str) -> None:
    """Append one evidence line. Never raises — evidence is never worth failing a run over."""
    # Imported here, not at module scope: `verify` imports this module and `record` imports
    # `verify`, so a top-level import would close a cycle. Nothing needs it before call time.
    from . import record

    try:
        entry = {
            "schema": SCHEMA,
            "cmd": record._clean(shlex.join(cmd), _MAX_CMD_CHARS),
            "exit": int(code),
            "started_at": started,
            "ended_at": ended,
            "cwd": os.getcwd(),
            # Same redaction table as the turn record — one policy, not two. `_clean`
            # redacts *then* truncates, which is the only order that can't leave the front
            # half of a secret on disk. Tail-kept before it gets here, so what survives the
            # char bound is still the end of the run.
            "output": record._clean(_tail(output, _MAX_OUTPUT_CHARS), _MAX_OUTPUT_CHARS),
        }
        path = path_for(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        _prune(path)
    except Exception:
        return


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _prune(path: Path) -> None:
    """Cap the log at `max_records()` lines, newest kept.

    Reuses the turn record's amortized pruner rather than growing a second retention policy
    that would drift from it — only the slack differs, because commands are logged far more
    often than turns are.
    """
    from . import record

    record._prune(path, max_records(), _PRUNE_SLACK)


def read(repo: Path, since: float = 0.0) -> tuple[CommandRun, ...]:
    """Every logged run that *started at or after* `since`, oldest first. Never raises.

    `since` is the staleness guard and it is not optional: this log outlives any one turn,
    session or harness, so an unbounded read would happily offer yesterday's green `pytest`
    as evidence for today's claim — a fabricated green, the one thing Tycho must never do.
    `verify.gather` supplies the floor; **`since=0.0` yields nothing**, because "no time
    anchor at all" is a reason to admit no evidence, not a reason to admit all of it.
    """
    if since <= 0.0:
        return ()
    runs = []
    try:
        with path_for(repo).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                run = _parse(line)
                if run is not None and run.started_at >= since:
                    runs.append(run)
    except OSError:
        return ()
    return tuple(runs)


def _parse(line: str) -> CommandRun | None:
    """One log line → a CommandRun, or None for a blank/corrupt/foreign-schema one.

    Skipping rather than raising, exactly as `record.iter_records` does: a half-written
    final line from a killed process must cost us that line, never the whole read.
    """
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("schema") != SCHEMA:
        return None
    try:
        return CommandRun(
            cmd=str(row["cmd"]),
            exit_code=int(row["exit"]),
            started_at=float(row["started_at"]),
            ended_at=float(row.get("ended_at") or row["started_at"]),
            cwd=str(row.get("cwd") or ""),
            output=str(row.get("output") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None

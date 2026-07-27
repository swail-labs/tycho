"""`tycho exec` — put a command's real output and exit status on the record (strategy §9.6).

Not "it closes our blind spots" — that is our problem, not the user's. The pitch is *"your
commands and their output are on the record, so the receipt is real."*

It is also what makes the dormant harnesses worth enabling: 3 of the 4 known misses in
`tests/test_eval.py` are structural, because the harness recorded no stdout or no exit
status. That gap never closes as models improve — it is a property of the harness, not of
the model — so the only fix is to capture the run ourselves.

Distinct from `tycho run`, which only *unmasks* an exit code for the transcript's benefit:
`exec` writes an independent evidence line Tycho owns.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import runlog, state

FILE = "commands.jsonl"


def path_for(repo: Path) -> Path:
    """`<repo>/.tycho/commands.jsonl` — the command evidence log."""
    return state.dir_for(repo) / FILE


def execute(repo: Path, argv: list[str]) -> int:
    """Run `argv`, forward its output and exit code unchanged, and log the evidence."""
    cmd = argv[1:] if argv and argv[0] == "--" else argv
    if not cmd:
        print("tycho exec: give a command, e.g. tycho exec -- pytest -q", file=sys.stderr)
        return 2
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"tycho exec: command not found: {cmd[0]}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    _log(repo, {
        "cmd": " ".join(cmd),
        "exit": proc.returncode,
        "started_at": started,
        "ended_at": time.time(),
        "outcome": runlog.outcome(proc.stdout + proc.stderr),
    })
    return proc.returncode


def _log(repo: Path, entry: dict) -> None:
    """Append one evidence line. Never raises — evidence is never worth failing a run over."""
    try:
        path = path_for(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass

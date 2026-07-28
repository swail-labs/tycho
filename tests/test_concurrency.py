"""Several agents, one repo, at the same time — the case that reproduced data loss.

Every test here spawns **real processes** (`subprocess`, not threads): the failures being
guarded against are `os.replace` publishing a splice of two writers, a prune dropping an
append it never saw, and a read-modify-write losing an increment. Threads in one process
share a GIL and an open-file table and prove nothing about any of them.

Each test is sized to finish in a couple of seconds and to fail loudly on the pre-fix code:
the numbers in the docstrings are what the unfixed version measured on this machine (macOS,
CPython 3.13).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import tycho

PKG_ROOT = str(Path(tycho.__file__).resolve().parent.parent)


def run_all(scripts: list[str], tmp_path: Path, timeout: float = 120) -> None:
    """Run every script at once and wait for all of them; raise on a non-zero exit."""
    procs = []
    for i, src in enumerate(scripts):
        path = tmp_path / f"worker{i}.py"
        path.write_text(textwrap.dedent(src), encoding="utf-8")
        procs.append(subprocess.Popen(
            [sys.executable, str(path)], cwd=PKG_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    for p in procs:
        out, err = p.communicate(timeout=timeout)
        assert p.returncode == 0, f"worker failed: {err or out}"


def preamble(tmp_path: Path) -> str:
    """Make `tycho` importable and keep the worker off the developer's real state."""
    return f"""
        import os, sys, time, json
        sys.path.insert(0, {PKG_ROOT!r})
        os.environ["TYCHO_HOME"] = {str(tmp_path / "home")!r}
        from pathlib import Path
        from tycho.store import record, state
        repo = Path({str(tmp_path / "repo")!r})
    """


# --- A1: a fixed `.tmp` name publishes a splice of two writers ----------------


def test_concurrent_json_writers_never_publish_a_corrupt_file(tmp_path: Path):
    """Two processes writing `<name>.tmp` with "w" and renaming it: the rename is atomic, but
    what gets renamed is half of each writer. Unfixed: 32,084 reads of non-JSON and 640 of an
    empty file over 6 seconds. `catches.json` corrupt resets the all-time tally; `vetoes.json`
    corrupt makes `state.vetoed()` return [] and a user's veto silently stops applying."""
    target = tmp_path / "repo" / ".tycho" / "vetoes.json"
    writers = [
        preamble(tmp_path) + f"""
        path = Path({str(target)!r})
        checks = ["check_{n}" + "x" * 400] * 40
        deadline = time.time() + 3.0
        while time.time() < deadline:
            state._write_json(path, {{"checks": checks}})
        """
        for n in range(3)
    ]
    reader = preamble(tmp_path) + f"""
        path = Path({str(target)!r})
        bad = 0
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not text:
                continue
            try:
                json.loads(text)
            except ValueError:
                bad += 1
        print(bad)
        sys.exit(1 if bad else 0)
        """
    run_all([*writers, reader], tmp_path)
    assert json.loads(target.read_text(encoding="utf-8"))["checks"]


def test_a_veto_survives_concurrent_writers(tmp_path: Path):
    """The verdict-integrity half of A1: a corrupt `vetoes.json` reads as *no vetoes*, so the
    agent's override applies again and a FAILED turn is relabelled OVERRIDDEN."""
    from tycho.store import state

    repo = tmp_path / "repo"
    state.veto_override(repo, "tests_pass")
    writers = [
        preamble(tmp_path) + """
        deadline = time.time() + 2.0
        while time.time() < deadline:
            state.veto_override(repo, "tests_pass")
        """
        for _ in range(3)
    ]
    reader = preamble(tmp_path) + """
        blank = 0
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if state.vetoed(repo) != ["tests_pass"]:
                blank += 1
        print(blank)
        sys.exit(1 if blank else 0)
        """
    run_all([*writers, reader], tmp_path)
    assert state.vetoed(repo) == ["tests_pass"]


# --- A2: the prune drops concurrent appends ----------------------------------


def test_appends_during_a_prune_are_not_lost(tmp_path: Path):
    """A pruner reads the whole file, writes a temp and renames it, so anything appended in
    between is gone. Unfixed: 65 records lost out of 8000 with one pruner and two appenders;
    with two pruners the file ended as a single 38KB corrupt line with all 6000 records gone.

    Fixed, every appender takes the lock the prune needs, so the count is exact."""
    per_worker, workers, cap = 150, 3, 200
    appenders = [
        preamble(tmp_path) + f"""
        os.environ["TYCHO_TURNS_MAX"] = "{cap}"
        record._PRUNE_SLACK = 0   # every append past the cap rewrites the whole file
        for i in range({per_worker}):
            rec = {{"schema": 1, "id": "{n}" * 16, "worker": {n}, "i": i,
                    "claims": ["c" * 200], "ended_at": float(i)}}
            assert record.append(repo, rec), "append reported failure"
            time.sleep(0.002)   # pace them, so all three are still running at the end
        """
        for n in range(workers)
    ]
    run_all(appenders, tmp_path)

    lines = record_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line) for line in lines), "a line on disk is not JSON"
    rows = [json.loads(line) for line in lines]
    assert all(r["claims"] == ["c" * 200] for r in rows), "a record on disk is spliced"
    # A prune drops the *oldest* records, so what survives of each worker is a contiguous
    # suffix of its run. A hole is an append that landed while a prune was rewriting.
    for n in range(workers):
        mine = sorted(r["i"] for r in rows if r["worker"] == n)
        assert mine, f"worker {n} lost every record"
        assert mine == list(range(mine[0], mine[0] + len(mine))), f"worker {n} has holes: {mine}"
        assert mine[-1] == per_worker - 1, f"worker {n} lost its newest record"


def test_no_record_is_lost_when_the_prune_never_fires(tmp_path: Path):
    """Same shape with the cap out of reach: now nothing may be dropped at all, so the count
    is exact and any loss is the appenders racing each other."""
    per_worker, workers = 200, 4
    appenders = [
        preamble(tmp_path) + f"""
        for i in range({per_worker}):
            assert record.append(repo, {{"schema": 1, "worker": {n}, "i": i}})
        """
        for n in range(workers)
    ]
    run_all(appenders, tmp_path)
    rows = [json.loads(line) for line in
            record_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == per_worker * workers
    assert {(r["worker"], r["i"]) for r in rows} == {
        (w, i) for w in range(workers) for i in range(per_worker)
    }


def record_path(tmp_path: Path) -> Path:
    return tmp_path / "repo" / ".tycho" / "turns.jsonl"


# --- A3: a truncated final line eats the next record too ---------------------


def test_a_killed_append_costs_one_record_not_two(tmp_path: Path):
    """A process killed mid-write leaves a line with no `\\n`; the next append lands on the
    same line and *both* records become one unparseable one. A harness that times the Stop
    hook out repeatedly loses every other record this way."""
    from tycho.store import record

    repo = tmp_path / "repo"
    record.append(repo, {"schema": 1, "id": "a", "ended_at": 1.0})
    path = record_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"schema":1,"id":"crash","ended_at":2.0')  # no newline: the crash shape
    record.append(repo, {"schema": 1, "id": "c", "ended_at": 3.0})

    ids = [r["id"] for r in record.iter_records(repo)]
    assert ids == ["a", "c"], "the record appended after a crashed one was lost too"


# --- A4: the tally loses increments ------------------------------------------


def test_the_machine_tally_counts_every_catch(tmp_path: Path):
    """`_bump` is read-modify-write, and `catches.json` at machine level is shared by every
    agent on the box. Unfixed: 4 processes × 500 catches recorded 47 of 2000."""
    per_worker, workers = 150, 4
    scripts = [
        preamble(tmp_path) + f"""
        for i in range({per_worker}):
            state.record_catch(repo, "claude", "FAILED", [])
        """
        for _ in range(workers)
    ]
    run_all(scripts, tmp_path)

    data = json.loads((tmp_path / "home" / "catches.json").read_text(encoding="utf-8"))
    assert data["tally"]["FAILED"] == per_worker * workers
    assert data["tally"]["runs"] == per_worker * workers


def test_the_relay_leash_cannot_be_exceeded_by_concurrent_hooks(tmp_path: Path):
    """Two hooks reading the same streak both write n+1, and the relay runs past its leash."""
    per_worker, workers = 40, 3
    scripts = [
        preamble(tmp_path) + f"""
        for i in range({per_worker}):
            state.bump_relay_streak(repo)
        """
        for _ in range(workers)
    ]
    run_all(scripts, tmp_path)

    from tycho.store import state
    assert state.relay_streak(tmp_path / "repo") == per_worker * workers


# --- the lock's own contract -------------------------------------------------


def test_a_stale_lock_is_stolen_rather_than_waited_out(tmp_path: Path, monkeypatch):
    """A process killed while holding the lock must not stop the next one for ever."""
    from tycho.store import state

    path = tmp_path / "f.json"
    lock = path.with_name(path.name + ".lock")
    lock.write_text("")
    old = time.time() - state._LOCK_STALE - 60
    import os
    os.utime(lock, (old, old))
    with state._locked(path, timeout=0.2) as held:
        assert held


def test_a_held_lock_does_not_block_the_turn(tmp_path: Path, monkeypatch):
    """Timing out on the lock still records the turn — losing a turn is worse than losing the
    prune, and nothing here may raise into the Stop hook."""
    from tycho.store import record, state

    monkeypatch.setattr(state, "_LOCK_TIMEOUT", 0.1)
    repo = tmp_path / "repo"
    path = record.path_for(repo)
    state._private_dir(path.parent)
    (path.parent / (path.name + ".lock")).write_text("")  # a fresh lock nobody will release
    try:
        assert record.append(repo, {"schema": 1, "id": "a"}, ) is True
    finally:
        (path.parent / (path.name + ".lock")).unlink()
    assert [r["id"] for r in record.iter_records(repo)] == ["a"]

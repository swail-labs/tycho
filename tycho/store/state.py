"""Tycho's own repo-local state under ``<repo>/.tycho/``: what `init` wired, and the
Stop hook's heartbeat. Separate files because they have separate writers.

Nothing here may raise into a caller — the hook must never break the agent's Stop.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Bump when an install no longer satisfies what init writes; `doctor` reports HOOK OUTDATED
# and routes the user to `tycho init`. Upgrading the package does not re-run init.
# 2 (0.2.0): prepare-commit-msg trailer hook + the `.gitignore` entry.
SCHEMA = 2

_DIR = ".tycho"
_INSTALL = "install.json"
_LAST_RUN = "last-run.json"

_CONFIG_MARKER = ".tycho.toml"
_GIT = ".git"


def root_for(repo: Path) -> Path:
    """`repo` or the nearest ancestor holding `.tycho/` or `.tycho.toml`, so a reader works
    from a subdirectory. Bounded by the git root and `$HOME` so a scratch directory never
    writes its turns into an unrelated project's ledger."""
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None
    for d in (repo, *repo.parents):
        # `$HOME` bounds the walk but is still honoured when it *is* `repo`.
        if d == home and d != repo:
            break
        if (d / _DIR).is_dir() or (d / _CONFIG_MARKER).is_file():
            return d
        if (d / _GIT).exists():  # repo root reached (a dir, or a worktree/submodule's file)
            break
    return repo


def dir_for(repo: Path) -> Path:
    return root_for(repo) / _DIR


def _read_json(path: Path) -> dict | None:
    """Our own state, so unreadable or corrupt means "unknown", never an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --- durable writes ----------------------------------------------
#
# Every writer here is concurrent — several agents share one repo. Shared with record.py and
# command.py so there is one policy: 0700/0600 on creation (turns.jsonl holds the agent's
# prose), a *per-writer* temp sibling (a fixed `.tmp` splices two writers together), and a
# lock for read-modify-write.

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_TIMEOUT = 5.0
_LOCK_STALE = 30.0  # a lock this old belonged to a process that died holding it


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)


def _touch_private(path: Path) -> None:
    """Create `path` 0600 if absent — the mode only applies on creation, hence not a chmod."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, _FILE_MODE))
    except OSError:
        pass


def _tmp_name(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


@contextmanager
def _locked(path: Path, timeout: float | None = None):
    """Hold an exclusive lock on `path`; yields True if acquired. Callers must work when it
    yields False — never block a turn on a lock.

    ponytail: an O_EXCL sidecar, one mechanism on all three platforms instead of flock +
    msvcrt. Ceiling: not atomic on old NFS, and a killed holder blocks until `_LOCK_STALE`.
    """
    lock = path.with_name(path.name + ".lock")
    fd = None
    deadline = time.monotonic() + (_LOCK_TIMEOUT if timeout is None else timeout)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        except OSError:
            break
    try:
        yield fd is not None
    finally:
        if fd is not None:
            try:
                os.close(fd)
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def _write_json(path: Path, data: dict) -> None:
    _private_dir(path.parent)
    tmp = _tmp_name(path)
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE),
                  "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    except OSError:
        # A lost write, never a corrupt file (Windows `replace` fails if the target is open).
        tmp.unlink(missing_ok=True)
        raise


def write_install(repo: Path, harness: str, command: str) -> None:
    """Record that we wired `harness` to `command`. Merges: other harnesses survive."""
    installed = dict(read_install(repo))
    installed[harness] = {"command": command, "at": time.time()}
    _write_json(dir_for(repo) / _INSTALL, {"schema": SCHEMA, "installed": installed})


def drop_install(repo: Path, harness: str) -> None:
    path = dir_for(repo) / _INSTALL
    installed = {k: v for k, v in read_install(repo).items() if k != harness}
    if installed:
        _write_json(path, {"schema": SCHEMA, "installed": installed})
        return
    path.unlink(missing_ok=True)
    (dir_for(repo) / _LAST_RUN).unlink(missing_ok=True)  # a heartbeat for nothing
    try:
        dir_for(repo).rmdir()  # only if empty — never take a dir holding someone's file
    except OSError:
        pass


def read_install(repo: Path) -> dict:
    """{harness: {"command": str, "at": float}} for what we believe we installed."""
    data = _read_json(dir_for(repo) / _INSTALL) or {}
    installed = data.get("installed")
    return installed if isinstance(installed, dict) else {}


def installed_schema(repo: Path) -> int | None:
    data = _read_json(dir_for(repo) / _INSTALL)
    return data.get("schema") if data else None


def record_run(repo: Path, harness: str, verdict: str | None = None, pending: bool = False) -> None:
    """The heartbeat, written on every hook invocation — a dead hook can't call this. Drives
    the badge: `pending` = yellow, `verdict` = green/red, neither = grey. Written on entry and
    at every terminal path, so nothing stale outlives its run."""
    beat: dict = {"at": time.time(), "harness": harness}
    if verdict is not None:
        beat["verdict"] = verdict
    elif pending:
        beat["pending"] = True
    try:
        _write_json(dir_for(repo) / _LAST_RUN, beat)
    except OSError:
        pass


# --- the catch record --------------------------------------------
#
#   repo    <repo>/.tycho/catches.json : {tally, catches: [entry, … newest first]}
#   machine <user_dir>/catches.json    : {tally}   — running total across every repo

_CATCHES = "catches.json"
_LEGACY_COUNTS = "counts.json"  # legacy tally; migrated on first read/write, then dropped
_TALLIED = ("FAILED", "STALE", "INDETERMINATE")  # verdicts that count as catches (with evidence)
_BLIND = ("INDETERMINATE", "UNSUPPORTED")  # verdicts where Tycho had nothing to say
_CATCH_LIST_CAP = 100  # the repo evidence trail keeps the most recent N; the tally stays exact


def user_dir() -> Path:
    """Machine-level state root: ``TYCHO_HOME``, ``XDG_DATA_HOME``, then ``~/.local/share``."""
    override = os.environ.get("TYCHO_HOME")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "tycho"


def _count_of(data: dict, key: str) -> int:
    """A counter off disk; anything unreadable means "no count yet"."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_catches(path: Path) -> dict:
    """catches.json, falling back to a legacy counts.json tally until the next write."""
    data = _read_json(path)
    if data is not None:
        return data
    legacy = _read_json(path.with_name(_LEGACY_COUNTS))
    if legacy:
        return {"tally": {v: _count_of(legacy, v) for v in _TALLIED if _count_of(legacy, v)}}
    return {}


def _tally_of(data: dict) -> dict:
    tally = data.get("tally")
    return tally if isinstance(tally, dict) else {}


def record_catch(repo: Path, harness: str, verdict: str, results) -> None:
    """Bump the tally (repo + machine); adverse verdicts also append to the evidence trail.
    Every verdict counts toward `runs` — VERIFIED/UNSUPPORTED are runs, not catches."""
    from . import record  # lazy: record imports state

    entry = None
    if verdict in _TALLIED:
        trail = [
            # Redacted like the turn record — this trail is durable too.
            {"check": r.name, "status": r.status.name,
             "evidence": record._clean(r.evidence, record._MAX_EVIDENCE_CHARS)}
            for r in results if r.status.name != "PASS"
        ]
        entry = {"at": time.time(), "harness": harness, "verdict": verdict, "checks": trail}
    _bump(dir_for(repo) / _CATCHES, verdict, entry)  # repo: tally (+ evidence if adverse)
    _bump(user_dir() / _CATCHES, verdict, None)      # machine: running tally only


def _bump(path: Path, verdict: str, entry: dict | None) -> None:
    # Locked: the machine file is shared by every agent on the box, and unlocked this dropped
    # 4 of every 5 increments under four concurrent writers.
    try:
        _private_dir(path.parent)
        with _locked(path):
            data = _read_catches(path)
            tally = _tally_of(data)
            tally["runs"] = _count_of(tally, "runs") + 1     # the denominator: every verdict
            tally[verdict] = _count_of(tally, verdict) + 1   # per-verdict, incl VERIFIED
            out: dict = {"tally": tally}
            if entry is not None:
                prior = data.get("catches") if isinstance(data.get("catches"), list) else []
                out["catches"] = [entry, *prior][:_CATCH_LIST_CAP]  # newest first, bounded
            _write_json(path, out)
            path.with_name(_LEGACY_COUNTS).unlink(missing_ok=True)  # migrated — drop the old
    except OSError:
        pass


def counts(repo: Path) -> dict:
    """{"FAILED": n, "STALE": n, "INDETERMINATE": n} — the running tally for `repo`."""
    tally = _tally_of(_read_catches(dir_for(repo) / _CATCHES))
    return {v: _count_of(tally, v) for v in _TALLIED}


def all_time_counts() -> dict:
    tally = _tally_of(_read_catches(user_dir() / _CATCHES))
    return {v: _count_of(tally, v) for v in _TALLIED}


def totals(repo: Path) -> dict:
    """{"runs": n, "blind": n} — the catch-count denominator. `runs` is 0 for a tally
    migrated from `counts.json`, which had none."""
    return _totals(_read_catches(dir_for(repo) / _CATCHES))


def all_time_totals() -> dict:
    return _totals(_read_catches(user_dir() / _CATCHES))


def _totals(data: dict) -> dict:
    tally = _tally_of(data)
    return {"runs": _count_of(tally, "runs"), "blind": sum(_count_of(tally, v) for v in _BLIND)}


def catches(repo: Path) -> list:
    """The evidence trail for `repo` — recent adverse/intermediate runs, newest first."""
    trail = _read_catches(dir_for(repo) / _CATCHES).get("catches")
    return trail if isinstance(trail, list) else []


def last_run(repo: Path) -> dict | None:
    return _read_json(dir_for(repo) / _LAST_RUN)


# --- the decay ledger (strategy §7/§9.5) --------------------------
#
# Per-check, per-model catch rate, so a competence-bound check gets retired with evidence.
# Sourced from `turns.jsonl` (catches.json has no per-check breakdown), so the window is
# `record.max_records()` turns. Two denominators, always reported together:
#   catch rate = caught / spoke — excludes turns it couldn't speak to.
#   blind rate = blind / seen   — doesn't improve with model capability; the §7 metric.

_CHECK_CAUGHT = ("FAIL", "STALE")  # per-check statuses that caught something
_CHECK_BLIND = ("UNSUPPORTED", "INDETERMINATE")  # per-check statuses with nothing to say
_RUN_CAUGHT = ("FAILED", "STALE")  # verdicts that count as a catch (matches `_caught` in cli)


def ledger(repo: Path) -> dict:
    """Per-check and per-model catch/blind rates over `repo`'s retained turn record::

        {"turns": int, "first": float|None, "last": float|None,
         "caught": int, "blind": int,
         "models": [{"model", "agent_version", "harness", "turns", "caught", "blind"}, …],
         "checks": [{"name", "spoke", "caught", "blind",
                     "models": [{"model", "spoke", "caught"}, …]}, …]}

    `model`/`agent_version` are None when the harness didn't expose them, never guessed; a
    null model is its own bucket. Sorted for a stable render."""
    from . import record  # lazy: record imports state

    turns = caught = blind = 0
    first = last = None
    per_model: dict[tuple, dict] = {}
    per_check: dict[str, dict] = {}
    for row in record.iter_records(repo):
        verdict = row.get("verdict")
        if not isinstance(verdict, str):
            continue  # not a turn record we can read
        turns += 1
        is_caught = verdict in _RUN_CAUGHT
        is_blind = verdict in _BLIND
        caught += is_caught
        blind += is_blind
        for stamp in ("started_at", "ended_at"):
            ts = row.get(stamp)
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
        key = (row.get("model"), row.get("agent_version"), row.get("harness"))
        bucket = per_model.setdefault(
            key,
            {"model": key[0], "agent_version": key[1], "harness": key[2],
             "turns": 0, "caught": 0, "blind": 0},
        )
        bucket["turns"] += 1
        bucket["caught"] += is_caught
        bucket["blind"] += is_blind
        for entry in row.get("checks") if isinstance(row.get("checks"), list) else ():
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            status = entry.get("status")
            check = per_check.setdefault(
                entry["name"], {"name": entry["name"], "spoke": 0, "caught": 0, "blind": 0,
                                "models": {}},
            )
            spoke = status not in _CHECK_BLIND
            check["spoke"] += spoke
            check["caught"] += status in _CHECK_CAUGHT
            check["blind"] += status in _CHECK_BLIND
            slice_ = check["models"].setdefault(key[0], {"model": key[0], "spoke": 0, "caught": 0})
            slice_["spoke"] += spoke
            slice_["caught"] += status in _CHECK_CAUGHT
    return {
        "turns": turns, "first": first, "last": last, "caught": caught, "blind": blind,
        "models": sorted(per_model.values(), key=lambda m: (-m["turns"], m["model"] or "")),
        "checks": [
            {**c, "models": sorted(c["models"].values(),
                                   key=lambda m: (-m["spoke"], m["model"] or ""))}
            for c in sorted(per_check.values(), key=lambda c: c["name"])
        ],
    }


# --- update check cache ------------------------------------------
# Machine-wide, one check serves every repo. An unusable cache just means "check again".

_UPDATE = "update-check.json"


def read_update_cache() -> dict:
    return _read_json(user_dir() / _UPDATE) or {}


def write_update_cache(**fields) -> None:
    try:
        data = read_update_cache()
        data.update(fields)
        _write_json(user_dir() / _UPDATE, data)
    except OSError:
        pass


def dismiss_update(version: str) -> None:
    write_update_cache(dismissed_version=version, dismissed=_count_of(read_update_cache(), "dismissed") + 1)


def update_dismissed_count() -> int:
    return _count_of(read_update_cache(), "dismissed")


# --- first-run offer bookkeeping ---------------------------------
# Machine-level, keyed by repo path: a declined offer writes nothing into that repo.

_OFFERED = "offered.json"


def already_offered(repo: Path) -> bool:
    data = _read_json(user_dir() / _OFFERED) or {}
    return _key(repo) in (data.get("repos") if isinstance(data.get("repos"), list) else [])


def mark_offered(repo: Path) -> None:
    try:
        data = _read_json(user_dir() / _OFFERED) or {}
        repos = data.get("repos") if isinstance(data.get("repos"), list) else []
        key = _key(repo)
        if key not in repos:
            repos.append(key)
        _write_json(user_dir() / _OFFERED, {"repos": repos})
    except OSError:
        pass


def _key(repo: Path) -> str:
    try:
        return str(repo.resolve())
    except OSError:
        return str(repo)


_STATUS_OFF = "status-off"


def status_enabled(repo: Path) -> bool:
    """Whether the status-bar badge renders here. Hiding it is not uninstalling."""
    return not (dir_for(repo) / _STATUS_OFF).exists()


def set_status_enabled(repo: Path, enabled: bool) -> None:
    path = dir_for(repo) / _STATUS_OFF
    try:
        if enabled:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    except OSError:
        pass


# --- verdict relay to the agent (opt-in, default OFF) --------------
#
# When ON, the Stop hook hands a non-VERIFIED verdict back to the agent so it works "until
# VERIFIED". The flag lives in `.tycho.toml` ([relay] enabled); the only runtime file here is
# the leash — `relay-streak` bounds auto-continuation to relay_max re-entries per user turn,
# so a verdict Tycho can never satisfy cannot cycle forever.

_RELAY_STREAK = "relay-streak"
_RELAY_MAX_DEFAULT = 3


def relay_max() -> int:
    """How many times the relay may auto-continue one user turn. ``TYCHO_RELAY_MAX``
    overrides, floored at 0 (= a single-shot notice); junk falls back to the default."""
    try:
        return max(0, int(os.environ.get("TYCHO_RELAY_MAX", _RELAY_MAX_DEFAULT)))
    except (TypeError, ValueError):
        return _RELAY_MAX_DEFAULT


def relay_enabled(repo: Path) -> bool:
    """`.tycho.toml` [relay] enabled, default off."""
    from . import config
    return config.load(repo).relay_enabled


def set_relay_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the verdict relay for `repo` in `.tycho.toml`, resetting the streak either way."""
    from . import config
    try:
        config.set_relay(repo, enabled)
    except OSError:
        pass
    reset_relay_streak(repo)


def relay_streak(repo: Path) -> int:
    """How many times the relay has already auto-continued the current user turn;
    absent/unreadable/garbage counts as 0."""
    try:
        return max(0, int((dir_for(repo) / _RELAY_STREAK).read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def bump_relay_streak(repo: Path) -> int:
    """Locked: two hooks reading the same value would each write n+1 and slip the leash."""
    path = dir_for(repo) / _RELAY_STREAK
    n = relay_streak(repo) + 1
    try:
        _private_dir(path.parent)
        with _locked(path):
            n = relay_streak(repo) + 1
            path.write_text(str(n), encoding="utf-8")
    except OSError:
        pass
    return n


def reset_relay_streak(repo: Path) -> None:
    try:
        (dir_for(repo) / _RELAY_STREAK).unlink(missing_ok=True)
    except OSError:
        pass


_STATUSLINE = "statusline.json"


def write_statusline_wrap(repo: Path, command: str, origin: str) -> None:
    """A status command `tycho status` runs and prepends its output to. `origin` "repo" means
    we replaced it and must restore it on uninstall; "user" means we never touched it."""
    _write_json(dir_for(repo) / _STATUSLINE, {"command": command, "origin": origin})


def read_statusline_wrap(repo: Path) -> dict | None:
    return _read_json(dir_for(repo) / _STATUSLINE)


def clear_statusline_wrap(repo: Path) -> None:
    (dir_for(repo) / _STATUSLINE).unlink(missing_ok=True)


# --- agent verdict override (opt-in, default OFF) ------------------
#
#   turn-marker <repo>/.tycho/override       : checks disputed THIS turn, cleared on prompt.
#   audit log   <repo>/.tycho/overrides.json : the permanent record.

_OVERRIDE_MARKER = "override"
_OVERRIDE_LOG = "overrides.json"
_OVERRIDE_LOG_CAP = 100
_VETOES = "vetoes.json"


def override_enabled(repo: Path) -> bool:
    """`.tycho.toml` [override] enabled, default off."""
    from . import config
    return config.load(repo).override_enabled


def set_override_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the capability, clearing any active marker either way."""
    from . import config
    try:
        config.set_override(repo, enabled)
    except OSError:
        pass
    clear_overrides(repo)
    try:
        (dir_for(repo) / _VETOES).unlink(missing_ok=True)
    except OSError:
        pass


def overrides(repo: Path) -> list:
    """The checks the agent has overridden this turn: [{"check": str, "reason": str}]."""
    data = _read_json(dir_for(repo) / _OVERRIDE_MARKER)
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def record_override(repo: Path, check: str, reason: str) -> None:
    """Record one per-check override in this turn's marker and in the audit log."""
    entry = {"at": time.time(), "check": check, "reason": reason}
    try:
        existing = overrides(repo)
        merged = [m for m in existing if m.get("check") != check]
        merged.append({"check": check, "reason": reason})
        _write_json(dir_for(repo) / _OVERRIDE_MARKER, {"entries": merged})
    except OSError:
        pass
    try:
        path = dir_for(repo) / _OVERRIDE_LOG
        data = _read_json(path) or {}
        prior = data.get("overrides") if isinstance(data.get("overrides"), list) else []
        _write_json(path, {"overrides": [entry, *prior][:_OVERRIDE_LOG_CAP]})
    except OSError:
        pass


def clear_overrides(repo: Path) -> None:
    """Drop this turn's override marker. The audit log stays."""
    try:
        (dir_for(repo) / _OVERRIDE_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def override_log(repo: Path) -> list:
    """The audit trail of overrides recorded in `repo`, newest first."""
    data = _read_json(dir_for(repo) / _OVERRIDE_LOG)
    trail = data.get("overrides") if isinstance(data, dict) else None
    return trail if isinstance(trail, list) else []


def vetoed(repo: Path) -> list:
    """Checks the user vetoed; an override for any is refused. Persists across turns."""
    data = _read_json(dir_for(repo) / _VETOES)
    checks = data.get("checks") if isinstance(data, dict) else None
    return checks if isinstance(checks, list) else []


def veto_override(repo: Path, check: str) -> None:
    """Countermand an override: drop it from the marker, add to the veto set, log it."""
    try:
        remaining = [m for m in overrides(repo) if m.get("check") != check]
        marker = dir_for(repo) / _OVERRIDE_MARKER
        if remaining:
            _write_json(marker, {"entries": remaining})
        else:
            marker.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        current = set(vetoed(repo))
        current.add(check)
        _write_json(dir_for(repo) / _VETOES, {"checks": sorted(current)})
    except OSError:
        pass
    try:
        path = dir_for(repo) / _OVERRIDE_LOG
        data = _read_json(path) or {}
        prior = data.get("overrides") if isinstance(data.get("overrides"), list) else []
        entry = {"at": time.time(), "check": check, "vetoed": True}
        _write_json(path, {"overrides": [entry, *prior][:_OVERRIDE_LOG_CAP]})
    except OSError:
        pass


def unveto_override(repo: Path, check: str) -> None:
    try:
        current = set(vetoed(repo))
        current.discard(check)
        path = dir_for(repo) / _VETOES
        if current:
            _write_json(path, {"checks": sorted(current)})
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass

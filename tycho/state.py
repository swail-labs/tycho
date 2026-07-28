"""Tycho's own repo-local state: what `init` wired up, and proof the hook still fires.

Two files under ``<repo>/.tycho/``, both entirely ours (no user content, ever):

- ``install.json``  — written by `tycho init`: schema version + what we wired, per harness.
- ``last-run.json`` — rewritten by the Stop hook on *every* invocation: the heartbeat.

Separate because init and the hook write from different processes on different schedules;
one shared file would mean read-modify-write races and lost updates.

**Nothing here may raise into a caller.** The hook must never break the agent's Stop (see
hook.py), so a heartbeat we can't write is simply not written and `tycho doctor` says
"unknown", which is honest.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Bump when an already-installed entry no longer satisfies what init now writes. `tycho
# doctor` compares this against install.json's stamp and reports HOOK OUTDATED on divergence.
#
# 2 (0.2.0): init gained the `prepare-commit-msg` trailer hook and the `.gitignore` entry.
# Neither exists in a 0.1.0 install, and upgrading the *package* does not re-run init — so
# without this bump `doctor` calls a half-installed repo healthy, `attest --verify` fails on
# every commit, and `turns.jsonl` (which holds the agent's own prose) accumulates in a
# directory nothing ignores. The bump is what routes an upgrader to `tycho init`, which is
# already idempotent and self-healing.
SCHEMA = 2

_DIR = ".tycho"
_INSTALL = "install.json"
_LAST_RUN = "last-run.json"

# config.py's file, but it marks the same root as `.tycho/` and can exist without it
# (configured, never inited), so root_for() looks for either.
_CONFIG_MARKER = ".tycho.toml"
_GIT = ".git"


def root_for(repo: Path) -> Path:
    """Where this repo's Tycho state lives: `repo` itself, or the nearest ancestor holding it.
    The walk exists because a cwd follows the user into subdirectories and every reader would
    otherwise report "not installed" from anywhere but the repo root. Stops at the git root so
    an unrelated parent's `.tycho/` is never adopted; no marker anywhere means `repo`."""
    for d in (repo, *repo.parents):
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


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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
    """The heartbeat, written on every hook invocation — a dead hook can't call this, so it is
    recorded even with nothing to verify. Three shapes drive the badge colour: `pending` =
    mid-run (yellow), `verdict` = the result (green/red), neither = nothing-to-report (grey).
    The hook calls this on entry and again at every terminal path, so a stale verdict or a
    stuck "verifying" can't outlive its run."""
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
# An entry is one adverse (FAILED/STALE) or intermediate (INDETERMINATE) run plus the checks
# that failed or couldn't pass. No transition dedup: a standing failure counts each turn.

_CATCHES = "catches.json"
_LEGACY_COUNTS = "counts.json"  # legacy tally; migrated on first read/write, then dropped
_TALLIED = ("FAILED", "STALE", "INDETERMINATE")  # verdicts that count as catches (with evidence)
_BLIND = ("INDETERMINATE", "UNSUPPORTED")  # verdicts where Tycho had nothing to say
_CATCH_LIST_CAP = 100  # the repo evidence trail keeps the most recent N; the tally stays exact


def user_dir() -> Path:
    """Root of Tycho's machine-level state, outside every repo. ``TYCHO_HOME``, then
    ``XDG_DATA_HOME``, then ``~/.local/share`` — the same chain as ``harness.home``."""
    override = os.environ.get("TYCHO_HOME")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "tycho"


def _count_of(data: dict, key: str) -> int:
    """A counter off disk, coerced; anything else on that key means "no count yet"."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_catches(path: Path) -> dict:
    """catches.json as {tally, catches}, falling back to a legacy counts.json tally until the
    next write re-homes it."""
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
    """Bump the running tally (repo + machine) and, when adverse or intermediate, append to
    the repo's evidence trail. Every verdict counts toward the `runs` denominator —
    VERIFIED/UNSUPPORTED are runs, not catches."""
    entry = None
    if verdict in _TALLIED:
        trail = [
            {"check": r.name, "status": r.status.name, "evidence": r.evidence}
            for r in results if r.status.name != "PASS"
        ]
        entry = {"at": time.time(), "harness": harness, "verdict": verdict, "checks": trail}
    _bump(dir_for(repo) / _CATCHES, verdict, entry)  # repo: tally (+ evidence if adverse)
    _bump(user_dir() / _CATCHES, verdict, None)      # machine: running tally only


def _bump(path: Path, verdict: str, entry: dict | None) -> None:
    # Read-modify-write with no lock: two repos catching in the same instant can lose one
    # machine-tally increment. A tally, not a ledger; add locking only if that ever matters.
    try:
        data = _read_catches(path)
        tally = _tally_of(data)
        tally["runs"] = _count_of(tally, "runs") + 1     # the denominator: every verdict
        tally[verdict] = _count_of(tally, verdict) + 1   # per-verdict, incl VERIFIED/UNSUPPORTED
        out: dict = {"tally": tally}
        if entry is not None:
            prior = data.get("catches") if isinstance(data.get("catches"), list) else []
            out["catches"] = [entry, *prior][:_CATCH_LIST_CAP]  # newest first, bounded
        _write_json(path, out)
        path.with_name(_LEGACY_COUNTS).unlink(missing_ok=True)  # migrated — drop the old file
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
    """{"runs": n, "blind": n} for `repo`: every verdict recorded (the denominator for the
    catch counts) and how many were blind. `runs` is 0 for a tally migrated from
    `counts.json`, which had no denominator."""
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
# Per-check, per-model catch rate over time, so a competence-bound check that reads zero
# across three model generations gets retired with evidence rather than by guess.
#
# Source of truth is `turns.jsonl`, not `catches.json` (a bag of counters with no per-check
# breakdown or attribution), so the ledger's window is `record.max_records()` turns and a
# legacy install with a tally but no turn record has an empty ledger — honest, and visible
# in the header the CLI prints.
#
# Two denominators, always reported together (`seen = spoke + blind`):
#   catch rate = caught / spoke — over turns where the check reached a verdict; excluding
#                                 ones it couldn't speak to stops a never-applicable check
#                                 looking good.
#   blind rate = blind / seen   — how often it had nothing to say; doesn't improve with model
#                                 capability, so it is the metric §7 wants.
# Run level reuses `counts`/`totals`' definitions, so `tycho count` and the ledger disagree
# only about the window, never about what a catch is.

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

    `model`/`agent_version` are None when the harness didn't expose them, never guessed, and
    a null model is its own bucket — merging it would corrupt this measurement. Sorted for a
    stable render: models by turn count then id, checks by name."""
    from . import record  # lazy: record imports state, so a module-level import would cycle

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
#
# Machine-wide (one check serves every repo): newest version seen, when we last looked, which
# version the user waved off, how many notices they've dismissed. An unusable cache just
# means "check again".

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
#
# Machine-level, keyed by repo path, so a declined offer is never re-nagged and nothing is
# written into a repo the user said no to.

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
    """Whether the status-bar indicator renders here. Default on; a sentinel file hides it.
    Hiding the badge is not uninstalling — the Stop hook keeps verifying every turn."""
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
# When ON, the Stop hook hands a non-VERIFIED verdict back to Claude as additionalContext, or
# to Codex as a blocked Stop with a continuation reason, so the agent works "until VERIFIED".
# Off by default: Tycho never feeds the agent its own context, or spends the extra
# generations, unless the user opts in.
#
# The flag lives in `.tycho.toml` ([relay] enabled); only the transient per-turn leash
# (`relay-streak`) is a runtime file here. That streak bounds auto-continuation to relay_max
# re-entries per user turn, so a verdict Tycho can never satisfy cannot cycle forever. It
# resets on every real user prompt and on VERIFIED.

_RELAY_STREAK = "relay-streak"
_RELAY_MAX_DEFAULT = 3


def relay_max() -> int:
    """How many times the relay may auto-continue one user turn. Default 3,
    ``TYCHO_RELAY_MAX`` overrides, floored at 0 (= a single-shot notice). Junk falls back to
    the default: this is read inside the Stop hook."""
    try:
        return max(0, int(os.environ.get("TYCHO_RELAY_MAX", _RELAY_MAX_DEFAULT)))
    except (TypeError, ValueError):
        return _RELAY_MAX_DEFAULT


def relay_enabled(repo: Path) -> bool:
    """Whether the Stop hook feeds the verdict back to the agent here — `.tycho.toml`
    ([relay] enabled), default off. Lazy import: config imports state."""
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
    n = relay_streak(repo) + 1
    try:
        path = dir_for(repo) / _RELAY_STREAK
        path.parent.mkdir(parents=True, exist_ok=True)
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
    """Record a status command Tycho composes with — `tycho status` runs it and prepends its
    output. `origin` "repo" is a foreign statusLine we replaced (restore it on uninstall);
    "user" is one we never touched, which resurfaces on its own."""
    _write_json(dir_for(repo) / _STATUSLINE, {"command": command, "origin": origin})


def read_statusline_wrap(repo: Path) -> dict | None:
    return _read_json(dir_for(repo) / _STATUSLINE)


def clear_statusline_wrap(repo: Path) -> None:
    (dir_for(repo) / _STATUSLINE).unlink(missing_ok=True)


# --- agent verdict override (opt-in, default OFF) ------------------
#
#   turn-marker <repo>/.tycho/override       : checks the agent disputes THIS turn, cleared
#                                              on UserPromptSubmit.
#   audit log   <repo>/.tycho/overrides.json : {overrides: [{at, check, reason}, …]}, the
#                                              permanent record.
# The on/off capability flag lives in `.tycho.toml` ([override] enabled), like [relay].

_OVERRIDE_MARKER = "override"
_OVERRIDE_LOG = "overrides.json"
_OVERRIDE_LOG_CAP = 100
_VETOES = "vetoes.json"


def override_enabled(repo: Path) -> bool:
    """Whether the agent may record verdict overrides here — `.tycho.toml`
    ([override] enabled), default off. Lazy import: config imports state."""
    from . import config
    return config.load(repo).override_enabled


def set_override_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the override capability in `.tycho.toml`, clearing any active marker either way."""
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
    """Checks the user has vetoed — an override for any is refused. Unlike the marker, these
    persist across turns until unvetoed or the capability is toggled."""
    data = _read_json(dir_for(repo) / _VETOES)
    checks = data.get("checks") if isinstance(data, dict) else None
    return checks if isinstance(checks, list) else []


def veto_override(repo: Path, check: str) -> None:
    """User countermands an override of `check`: drop it from this turn's marker, add it to
    the persistent veto set, log the veto."""
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

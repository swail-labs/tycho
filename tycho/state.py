"""Tycho's own repo-local state: what `init` wired up, and proof the hook still fires.

Two files under ``<repo>/.tycho/``, both entirely ours (no user content, ever):

- ``install.json``  — written by `tycho init`: schema version + what we wired, per harness.
- ``last-run.json`` — rewritten by the Stop hook on *every* invocation: the heartbeat.

Separate files on purpose: init and the hook write on different schedules from
different processes, so one shared file would mean read-modify-write races and a lost
update — exactly the kind of silent corruption TYCHO-6 just finished stamping out.

**Nothing here may raise into a caller.** The hook must never break the agent's Stop
(see hook.py), so a heartbeat we can't write is simply not written: a missing beat makes
`tycho doctor` say "unknown", which is honest, whereas an exception here would break the
one thing Tycho promises never to touch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Bump when the shape of what init installs changes such that an already-installed
# entry no longer satisfies it. `tycho doctor` compares this against the stamp in
# install.json and reports HOOK OUTDATED when they diverge; `tycho init` re-stamps.
SCHEMA = 1

_DIR = ".tycho"
_INSTALL = "install.json"
_LAST_RUN = "last-run.json"

# `.tycho.toml` is config.py's file, but it marks the same root as `.tycho/` and can exist
# without it (configured, never inited), so root_for() looks for either. Spelled out here
# rather than imported to keep state.py free of a config import it otherwise has no use for.
_CONFIG_MARKER = ".tycho.toml"
_GIT = ".git"


def root_for(repo: Path) -> Path:
    """Where this repo's Tycho state lives: `repo` itself, or the nearest ancestor holding it.

    Every entry point hands us a cwd, and a cwd follows the user into subdirectories. Without
    the walk, `<cwd>/.tycho` misses from anywhere but the repo root, and every reader here
    reports that as "not installed" — the badge goes blank and `doctor` says unknown, both of
    which read as "Tycho isn't here" rather than "you're one directory down".

    Stops at the git root: our state belongs to *this* repo, and an unrelated parent's `.tycho/`
    is not ours to adopt. No marker anywhere means `repo` — so `init` still creates state where
    it was run, and a first write lands exactly where it does today.
    """
    for d in (repo, *repo.parents):
        if (d / _DIR).is_dir() or (d / _CONFIG_MARKER).is_file():
            return d
        if (d / _GIT).exists():  # repo root reached (a dir, or a worktree/submodule's file)
            break
    return repo


def dir_for(repo: Path) -> Path:
    return root_for(repo) / _DIR


def _read_json(path: Path) -> dict | None:
    """Our own state, so unreadable or corrupt means "unknown" — never an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    """Atomic, like every other write in this package: temp sibling, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_install(repo: Path, harness: str, command: str) -> None:
    """Record that we wired `harness` to `command`. Merges — other harnesses survive."""
    installed = dict(read_install(repo))
    installed[harness] = {"command": command, "at": time.time()}
    _write_json(dir_for(repo) / _INSTALL, {"schema": SCHEMA, "installed": installed})


def drop_install(repo: Path, harness: str) -> None:
    """Forget one harness; drop the file (and dir) once there's nothing left to remember."""
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
    """The schema stamp on disk, or None if we've never installed here."""
    data = _read_json(dir_for(repo) / _INSTALL)
    return data.get("schema") if data else None


def record_run(repo: Path, harness: str, verdict: str | None = None, pending: bool = False) -> None:
    """The heartbeat. Called on every hook invocation — a dead hook can't call this.

    Recorded even when the hook finds nothing to verify: the question this answers is
    "did the wiring fire?", not "was there a verdict?". Swallows everything, per the
    module docstring.

    The hook calls this at least twice: once on entry with ``pending=True`` (the beat must
    land even if everything downstream fails — and the badge shows "verifying"), then again
    at every terminal path — with a `verdict` when there is one, or plain (no verdict, no
    pending) when there was nothing to verify. So the three shapes are distinct and drive
    the badge colour: `pending` = mid-run (yellow), `verdict` = the result
    (green/red), neither = fired-but-nothing-to-report (grey). Re-recording is what stops a
    stale verdict — or a stuck "verifying" — outliving the run that produced it.
    """
    beat: dict = {"at": time.time(), "harness": harness}
    if verdict is not None:
        beat["verdict"] = verdict
    elif pending:
        beat["pending"] = True
    try:
        _write_json(dir_for(repo) / _LAST_RUN, beat)
    except OSError:
        pass


# --- the catch record (replaces TYCHO-50's bare tally) -------------
#
# What Tycho caught — with the evidence, not just a number. Two files, same fail-open rule
# as everything here (a record we can't write is simply not written):
#   repo   <repo>/.tycho/catches.json : {tally, catches: [entry, … newest first]}
#   machine <user_dir>/catches.json   : {tally}   — running total across every repo
# An entry is one adverse (FAILED/STALE) or intermediate (INDETERMINATE) run plus the
# checks that failed or couldn't pass. The last *verdict* is not stored here — that lives
# in last-run.json — so there is no transition dedup: every adverse/intermediate run is
# recorded (a standing failure re-reported each turn counts each turn).

_CATCHES = "catches.json"
_LEGACY_COUNTS = "counts.json"  # pre-TYCHO-62 tally; migrated on first read/write, then dropped
_TALLIED = ("FAILED", "STALE", "INDETERMINATE")  # verdicts that count as catches (with evidence)
_BLIND = ("INDETERMINATE", "UNSUPPORTED")  # verdicts where Tycho had nothing to say
_CATCH_LIST_CAP = 100  # the repo evidence trail keeps the most recent N; the tally stays exact


def user_dir() -> Path:
    """Root of Tycho's *machine-level* state — outside every repo, by definition.

    ``TYCHO_HOME`` wins, then ``XDG_DATA_HOME``, then the ``~/.local/share`` default:
    the same override chain the harness roots use (``harness.home``, TYCHO-14), spelled
    out here because this root is an XDG data dir rather than a ``~/.<name>`` dotdir.
    """
    override = os.environ.get("TYCHO_HOME")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "tycho"


def _count_of(data: dict, key: str) -> int:
    """A counter off disk, coerced. Anything else on that key means "no count yet"."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_catches(path: Path) -> dict:
    """catches.json as {tally, catches}. Falls back to a legacy counts.json tally so the
    numbers carry across the rename, until the next write re-homes them."""
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
    """Record one verdict: bump the running tally (repo + machine) and, when the verdict is
    adverse/intermediate, append it to the repo's evidence trail. *Every* verdict counts
    toward the `runs` denominator — VERIFIED/UNSUPPORTED are runs, not catches, so
    they add no evidence entry. Never raises: this runs inside the Stop hook, so a record we
    can't write is simply not written.
    """
    entry = None
    if verdict in _TALLIED:
        # The evidence trail: what failed or couldn't pass. Every non-PASS check, with its
        # status and the same evidence string the report shows.
        trail = [
            {"check": r.name, "status": r.status.name, "evidence": r.evidence}
            for r in results if r.status.name != "PASS"
        ]
        entry = {"at": time.time(), "harness": harness, "verdict": verdict, "checks": trail}
    _bump(dir_for(repo) / _CATCHES, verdict, entry)  # repo: tally (+ evidence if adverse)
    _bump(user_dir() / _CATCHES, verdict, None)      # machine: running tally only


def _bump(path: Path, verdict: str, entry: dict | None) -> None:
    # read-modify-write with no lock — two repos catching something in the same
    # instant can lose one machine-tally increment. It's a tally, not a ledger; add locking
    # only if a slightly-low all-time number ever actually matters to someone.
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
    """The same running tally across every repo on this machine."""
    tally = _tally_of(_read_catches(user_dir() / _CATCHES))
    return {v: _count_of(tally, v) for v in _TALLIED}


def totals(repo: Path) -> dict:
    """{"runs": n, "blind": n} for `repo`: every verdict recorded — the denominator
    the catch counts are read against — and how many of those were blind (INDETERMINATE or
    UNSUPPORTED, i.e. Tycho had nothing to say). `runs` is 0 for a legacy tally migrated from
    the pre-TYCHO-58 `counts.json`, which had no denominator; it fills in as new runs land."""
    return _totals(_read_catches(dir_for(repo) / _CATCHES))


def all_time_totals() -> dict:
    """The same runs/blind totals across every repo on this machine."""
    return _totals(_read_catches(user_dir() / _CATCHES))


def _totals(data: dict) -> dict:
    tally = _tally_of(data)
    return {"runs": _count_of(tally, "runs"), "blind": sum(_count_of(tally, v) for v in _BLIND)}


def catches(repo: Path) -> list:
    """The evidence trail for `repo` — recent adverse/intermediate runs, newest first."""
    trail = _read_catches(dir_for(repo) / _CATCHES).get("catches")
    return trail if isinstance(trail, list) else []


def last_run(repo: Path) -> dict | None:
    """The last recorded heartbeat, or None if the hook has never fired here."""
    return _read_json(dir_for(repo) / _LAST_RUN)


# --- the decay ledger (strategy §7/§9.5) --------------------------
#
# Five of the nine checks are competence-bound and will stop firing as agents get better
# (§7). That is a maintenance problem only if you can *see* it, so this is the instrument:
# per-check, per-model catch rate over time, from which a check that reads zero across
# three model generations gets retired publicly, with evidence, instead of by guess.
#
# **Source of truth: `turns.jsonl`, not `catches.json`.** The tally in catches.json is a
# bag of counters — it has no per-check breakdown and no place to hang attribution without
# multiplying into (model × check × status) counters that would then be a second, drifting
# copy of what the turn record already holds exactly. `record.py` already stamps `model`,
# `agent_version`, `harness` and every per-check status on every line, so the ledger reads
# that and catches.json keeps doing the one thing it does well: an exact, unbounded,
# machine-wide *tally*. The two answer different questions and the ledger says so out loud
# (`turns` is the retained record; `runs` in `count` is all-time), rather than pretending a
# capped file and an uncapped counter are the same number.
#
# Consequence, stated rather than hidden: the ledger's window is `record.max_records()`
# turns (default 5000), and a legacy install with a tally but no turn record has an empty
# ledger. Both are honest — "we don't have that evidence" is the same posture as
# INDETERMINATE — and both are visible in the header the CLI prints.
#
# **Denominators.** Two rates, never one without the other, because they answer different
# questions and only one of them is a competence signal:
#
#   catch rate = caught / spoke   — of the turns where the check had enough evidence to
#                                   reach a verdict (PASS/FAIL/STALE), how often did it
#                                   find something. Excludes turns the check couldn't
#                                   speak to, because counting those as "didn't catch"
#                                   flatters a check that is merely never applicable.
#   blind rate = blind / seen     — of every turn the check ran in, how often it had
#                                   nothing to say (UNSUPPORTED/INDETERMINATE). This is
#                                   the harness/evidence metric §7 promotes: it does not
#                                   improve with model capability.
#
# `seen = spoke + blind`, so the two denominators are stated, not inferred, and a check
# that is UNSUPPORTED on 90% of turns shows a small `spoke` next to a 90% blind rate —
# you cannot read one rate without the other being on the same line.
#
# Run level uses the same words for the same things: a turn is `caught` when its verdict
# is FAILED/STALE and `blind` when it is INDETERMINATE/UNSUPPORTED, over all recorded
# turns. Deliberately the same definitions `counts`/`totals` use above, so `tycho count`
# and the ledger never disagree about what a catch is — only about the window.

_CHECK_CAUGHT = ("FAIL", "STALE")  # per-check statuses that caught something
_CHECK_BLIND = ("UNSUPPORTED", "INDETERMINATE")  # per-check statuses with nothing to say
_RUN_CAUGHT = ("FAILED", "STALE")  # verdicts that count as a catch (matches `_caught` in cli)


def ledger(repo: Path) -> dict:
    """Per-check and per-model catch/blind rates over `repo`'s retained turn record.

    Returns (all counts are turns)::

        {"turns": int, "first": float|None, "last": float|None,
         "caught": int, "blind": int,
         "models": [{"model", "agent_version", "harness", "turns", "caught", "blind"}, …],
         "checks": [{"name", "spoke", "caught", "blind",
                     "models": [{"model", "spoke", "caught"}, …]}, …]}

    `model`/`agent_version` are None when the harness didn't expose them — never guessed,
    never backfilled (see `model.Attribution`); a null model is its own bucket rather than
    being folded into a neighbour, because "we don't know which model did this" is a fact
    about the evidence and merging it would corrupt exactly the measurement this exists for.

    Sorted for a stable render: models by turn count then id, checks by name. Never raises
    and never opens a socket — this reads one local file, like everything else here.
    """
    from . import record  # lazy: record imports state, so a module-level import would cycle

    turns = caught = blind = 0
    first = last = None
    per_model: dict[tuple, dict] = {}
    per_check: dict[str, dict] = {}
    for row in record.iter_records(repo):
        verdict = row.get("verdict")
        if not isinstance(verdict, str):
            continue  # not a turn record we can read — skip it, same rule as a corrupt line
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
# Machine-wide (one check serves every repo): the newest version we saw, when we last
# looked, which version the user waved off, and how many times they've dismissed a notice.
# Fail-open like the rest — an unreadable/​unwritable cache just means "check again".

_UPDATE = "update-check.json"


def read_update_cache() -> dict:
    return _read_json(user_dir() / _UPDATE) or {}


def write_update_cache(**fields) -> None:
    """Merge `fields` into the update cache. Never raises."""
    try:
        data = read_update_cache()
        data.update(fields)
        _write_json(user_dir() / _UPDATE, data)
    except OSError:
        pass


def dismiss_update(version: str) -> None:
    """Record that the user waved off the notice for `version`, counting the dismissal."""
    write_update_cache(dismissed_version=version, dismissed=_count_of(read_update_cache(), "dismissed") + 1)


def update_dismissed_count() -> int:
    """How many update notices the user has dismissed (record-keeping, TYCHO-53)."""
    return _count_of(read_update_cache(), "dismissed")


# --- first-run offer bookkeeping ---------------------------------
#
# Machine-level, keyed by repo path: which repos we've already made the "set up Tycho here?"
# offer in, so a declined offer is never re-nagged — and nothing is written into a repo the
# user said no to (the marker lives outside it).

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
    """Whether the status-bar indicator should render here. Default on; a sentinel hides it.

    Hiding the badge is not uninstalling: the Stop hook keeps verifying every turn, the
    heartbeat keeps landing — only the passive indicator goes quiet.
    """
    return not (dir_for(repo) / _STATUS_OFF).exists()


def set_status_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the indicator on/off for `repo`. Never raises (same fail-open rule as above)."""
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
# Off by default: Tycho never feeds the agent its own context — and never spends the extra
# generations doing so — unless the user opts in. When ON, the Stop hook hands a non-VERIFIED
# verdict back to Claude as additionalContext or Codex as a blocked Stop with a continuation
# reason, which makes the agent address what Tycho caught, "until VERIFIED".
#
# The on/off flag lives in `.tycho.toml` (`[relay] enabled`, TYCHO-114) so it's hand-editable
# and versionable; these functions just delegate to `config`. Only the transient per-turn leash
# (`relay-streak`) stays a runtime file here — it's ephemeral state, not user configuration.
#
# That auto-continuation is *bounded* by the streak counter: one user turn can only be re-entered
# a fixed number of times (relay_max) before Tycho goes quiet and hands control back — so a
# verdict Tycho can never satisfy cannot cycle forever (the operator's explicit requirement).
# The streak resets on every real user prompt (UserPromptSubmit) and on a VERIFIED verdict.

_RELAY_STREAK = "relay-streak"
_RELAY_MAX_DEFAULT = 3


def relay_max() -> int:
    """How many times the relay may auto-continue one user turn before going quiet.

    Default 3; ``TYCHO_RELAY_MAX`` overrides it for a longer or shorter leash. Floored at 0
    (0 = never force a continuation — the relay becomes a pure, single-shot notice). A junk
    value falls back to the default rather than raising: this is read inside the Stop hook.
    """
    try:
        return max(0, int(os.environ.get("TYCHO_RELAY_MAX", _RELAY_MAX_DEFAULT)))
    except (TypeError, ValueError):
        return _RELAY_MAX_DEFAULT


def relay_enabled(repo: Path) -> bool:
    """Whether the Stop hook should feed the verdict back to the agent here. Reads
    `.tycho.toml` ([relay] enabled), default off. Lazy import of `config` avoids an import
    cycle (config imports state for its path resolution)."""
    from . import config
    return config.load(repo).relay_enabled


def set_relay_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the verdict relay for `repo` in `.tycho.toml`. Resets the streak either way (a
    toggle starts a fresh leash). Never raises — same fail-open rule as every write here."""
    from . import config
    try:
        config.set_relay(repo, enabled)
    except OSError:
        pass
    reset_relay_streak(repo)


def relay_streak(repo: Path) -> int:
    """How many times the current user turn has already been auto-continued by the relay.
    Absent/unreadable/garbage counts as 0 — the honest floor for "nothing recorded yet"."""
    try:
        return max(0, int((dir_for(repo) / _RELAY_STREAK).read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def bump_relay_streak(repo: Path) -> int:
    """Record one more relay auto-continuation; return the new count. Never raises."""
    n = relay_streak(repo) + 1
    try:
        path = dir_for(repo) / _RELAY_STREAK
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(n), encoding="utf-8")
    except OSError:
        pass
    return n


def reset_relay_streak(repo: Path) -> None:
    """Clear the auto-continuation count — a fresh user prompt, a VERIFIED verdict, or a toggle."""
    try:
        (dir_for(repo) / _RELAY_STREAK).unlink(missing_ok=True)
    except OSError:
        pass


_STATUSLINE = "statusline.json"


def write_statusline_wrap(repo: Path, command: str, origin: str) -> None:
    """Record a status command Tycho should compose with.

    `origin` is "repo" (a foreign statusLine in this repo's own settings that we replaced —
    restore it on uninstall) or "user" (a user-level line we never touched — it resurfaces
    on its own when ours is removed). `tycho status` runs `command` and prepends its output.
    """
    _write_json(dir_for(repo) / _STATUSLINE, {"command": command, "origin": origin})


def read_statusline_wrap(repo: Path) -> dict | None:
    """The recorded compose target, or None. Our own state, so unreadable means "none"."""
    return _read_json(dir_for(repo) / _STATUSLINE)


def clear_statusline_wrap(repo: Path) -> None:
    (dir_for(repo) / _STATUSLINE).unlink(missing_ok=True)


# --- agent verdict override (opt-in, default OFF) ------------------
#
# Two artifacts, both fail-open like everything here:
#   turn-marker  <repo>/.tycho/override        : the checks the agent disputes THIS turn,
#                                                 cleared on UserPromptSubmit (a fresh turn).
#   audit log    <repo>/.tycho/overrides.json  : {overrides: [{at, check, reason}, … newest first]},
#                                                 written at record time — the permanent record.
# The on/off capability flag lives in `.tycho.toml` ([override] enabled), like [relay].

_OVERRIDE_MARKER = "override"
_OVERRIDE_LOG = "overrides.json"
_OVERRIDE_LOG_CAP = 100
_VETOES = "vetoes.json"


def override_enabled(repo: Path) -> bool:
    """Whether the agent may record verdict overrides here. Reads `.tycho.toml`
    ([override] enabled), default off. Lazy import of config avoids the import cycle."""
    from . import config
    return config.load(repo).override_enabled


def set_override_enabled(repo: Path, enabled: bool) -> None:
    """Toggle the override capability in `.tycho.toml`. Clears any active marker either way
    (a toggle starts clean). Never raises — same fail-open rule as every write here."""
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
    """The checks the agent has overridden this turn: [{"check": str, "reason": str}].
    Absent/unreadable/garbage → [] (the honest floor)."""
    data = _read_json(dir_for(repo) / _OVERRIDE_MARKER)
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def record_override(repo: Path, check: str, reason: str) -> None:
    """Record one per-check override: add it to this turn's marker AND append it to the audit
    log. Never raises — this is called from a deliberate CLI action, but stays fail-open."""
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
    """Drop this turn's override marker — a fresh user prompt or a toggle. The audit log stays."""
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
    """Checks the user has vetoed — an override for any of these is refused/ignored. Persists
    across turns (unlike the marker) until unvetoed or the capability is toggled. [] when none."""
    data = _read_json(dir_for(repo) / _VETOES)
    checks = data.get("checks") if isinstance(data, dict) else None
    return checks if isinstance(checks, list) else []


def veto_override(repo: Path, check: str) -> None:
    """User countermands an override of `check`: drop it from this turn's marker, add it to the
    persistent veto set (so it can't be re-applied), and log the veto. Never raises."""
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
    """Lift a veto so `check` may be overridden again. Never raises."""
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

"""`tycho doctor` — is Tycho actually wired, current, and still firing here?

A dead hook cannot report its own death: it runs no code. So liveness comes from the
heartbeat `state.record_run` writes on every invocation, which `doctor` reads back. Limits:
a heartbeat proves the hook fired at that time, never current health; **no heartbeat is not
proof of death** (a fresh install hasn't fired yet, and a diagnostic that cries wolf gets
ignored); nothing polls, so a hook that died five minutes ago goes undiagnosed until asked.
Doctor reports what it can prove from disk, says "unknown" where it can't, and never edits
anything — `tycho init` is the (self-healing) repair.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from . import gitstate
from . import harness as harness_mod
from . import install as init_mod
from . import state
from . import version as version_mod
from .review import _ago

# Severities. BROKEN and OUTDATED mean "Tycho is not doing its job"; the rest is context.
OK = "OK"
BROKEN = "HOOK BROKEN"
OUTDATED = "HOOK OUTDATED"
INFO = "INFO"
# Advisory only (not in _ADVERSE), but louder than INFO: whether the harness still reads
# our output is the one thing doctor structurally can't check for itself.
DRIFT = "HARNESS DRIFT"
# `.tycho/` is exposed to git. Not adverse (Tycho is still verifying), but the turn record
# carries the agent's own prose and every command it ran, so it outranks INFO.
EXPOSED = "TURN RECORD NOT IGNORED"
TRACKED = "TURN RECORD TRACKED BY GIT"

_ADVERSE = (BROKEN, OUTDATED)


@dataclass(frozen=True)
class Finding:
    """One diagnosis. `fix` is mandatory for adverse levels — a complaint without a
    remedy is noise."""

    level: str
    text: str
    fix: str = ""


def hook_health(repo: Path) -> list[Finding]:
    """Only the adverse wiring findings: is what's installed runnable, and current?

    No discovery, no heartbeat — cheap enough to run on *every* manual command."""
    findings: list[Finding] = []
    _wired_harnesses(repo, findings)
    return adverse(findings)


def _wired_harnesses(repo: Path, findings: list[Finding]) -> list[str]:
    recorded = state.read_install(repo)
    wired = [n for n in init_mod.HARNESSES if _is_wired(repo, n, recorded, findings)]
    schema = _schema_finding(repo)
    if schema is not None:
        findings.append(schema)
    return wired


def diagnose(repo: Path) -> list[Finding]:
    """Everything we can establish about this repo's wiring, without touching it."""
    findings: list[Finding] = []
    # doctor is manual, so the fail-open network check is fine here.
    note = version_mod.notice(refresh_first=True, force=True)  # explicit command — no cache
    if note:
        findings.append(Finding(INFO, note, "run `tycho update`"))
    wired = _wired_harnesses(repo, findings)
    findings.extend(_exposure(repo))
    findings.extend(_upgrade_notes(repo))

    if not wired:
        findings.append(Finding(
            INFO, "no Tycho hook is installed in this repo", "run `tycho init` to install one"
        ))
        return findings

    findings.append(_heartbeat_finding(repo, wired))
    findings.append(_transcript_finding(repo))
    findings.extend(_harness_drift(wired))
    return findings


def adverse(findings: list[Finding]) -> list[Finding]:
    """The findings that mean Tycho isn't doing its job."""
    return [f for f in findings if f.level in _ADVERSE]


def liveness(repo: Path) -> str:
    """One line answering "is it on here?", for `tycho help`. Never raises — it degrades to
    an honest "unknown"; `tycho doctor` stays the full answer."""
    try:
        findings: list[Finding] = []
        wired = _wired_harnesses(repo, findings)
        if not wired:
            return "NOT installed here — run `tycho init`"
        broken = adverse(findings)
        if broken:
            return f"installed, but not working — {broken[0].text}"
        return f"installed ({', '.join(wired)}) — {_heartbeat_finding(repo, wired).text}"
    except Exception as exc:  # a status line must never be the reason `tycho help` fails
        return f"status unknown ({type(exc).__name__}) — run `tycho doctor`"


def _is_wired(repo: Path, name: str, recorded: dict, findings: list[Finding]) -> bool:
    """Diagnose one harness against its *own config*: install.json is a claim, the
    harness's config is what decides whether Tycho fires."""
    try:
        command = init_mod.installed_command(repo, name)
    except init_mod.ConfigRefused as exc:
        findings.append(Finding(BROKEN, f"{name}: {exc}", "fix that file, then re-run `tycho init`"))
        return False

    if command is None:
        if name in recorded:
            # We installed it and it's gone (an upgrade or a hand-edit rewrote the config).
            # Loud, because the user believes they're covered and isn't.
            findings.append(Finding(
                BROKEN,
                f"{name}: Tycho installed a hook here, but it's gone from {init_mod.config_path(repo, name)}",
                "run `tycho init` to reinstall it",
            ))
        return False

    if not _resolves(command):
        # The config looks installed but the harness runs it into a missing interpreter —
        # the exact silent death this command exists to surface.
        findings.append(Finding(
            BROKEN,
            f"{name}: hook command doesn't resolve to a runnable program — {command}",
            "run `tycho init` to rewrite it for this environment",
        ))
        return True  # wired (the entry exists), just not working

    # Not compared against `init.hook_command()`: that answer depends on whether the venv is
    # on PATH right now, so one healthy install would read as two different "current"s.
    findings.append(Finding(OK, f"{name}: hook installed and runnable — {command}"))
    return True


def _resolves(command: str) -> bool:
    """Would the host shell find something to execute here?

    Split the way the *host* shell would (Windows keeps backslashes; POSIX escapes them).
    The executable bit is POSIX-only — on Windows, existence is the runnable test."""
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return False
    if not argv:
        return False
    program = argv[0].strip('"')
    is_path = bool(os.path.dirname(program)) or (os.name == "nt" and ":" in program)
    if is_path:
        if os.name == "nt":
            return os.path.isfile(program) or shutil.which(program) is not None
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return shutil.which(program) is not None


def _schema_finding(repo: Path) -> Finding | None:
    stamped = state.installed_schema(repo)
    if stamped is None:
        return None  # installed before we kept state, or state was cleaned — not a fault
    if stamped != state.SCHEMA:
        return Finding(
            OUTDATED,
            f"installed hook config is schema v{stamped}; this Tycho speaks v{state.SCHEMA}",
            "run `tycho init` to rewrite it",
        )
    return None


def _exposure(repo: Path) -> list[Finding]:
    """Is `.tycho/` exposed to git here?

    0.1.0 shipped no gitignore step, so an upgraded repo can be writing `turns.jsonl` — the
    agent's prose, every command it ran, and check evidence — into a directory `git add -A`
    will happily commit. Two distinct faults: not ignored (init fixes it) and already tracked
    (init cannot: an ignore rule does not untrack what git already follows).

    Fails silent on every "can't tell": no git, not a repo, unreadable index. A privacy alarm
    that fires on a machine without git is a privacy alarm nobody reads."""
    entry = init_mod._IGNORE_ENTRY
    if not gitstate.is_repo(repo):
        return []
    findings: list[Finding] = []
    code, out = gitstate._git(repo, "ls-files", "--error-unmatch", "--", entry)
    if code == 0 and out.strip():
        findings.append(Finding(
            TRACKED,
            f"{entry} is tracked by git — the turn record (the agent's prose, its commands and "
            f"check evidence) is being committed and pushed",
            "untrack it yourself, then commit: `git rm -r --cached .tycho/` — `tycho init` "
            "cannot repair this, and Tycho will not run it for you (already-pushed history "
            "stays exposed; that call is yours)",
        ))
    ignored, _ = gitstate._git(repo, "check-ignore", "-q", entry)
    if ignored == 1:  # 0 = ignored, 128 = git couldn't tell → say nothing
        findings.append(Finding(
            EXPOSED,
            f"{entry} is not gitignored here — `git add -A` will pick up the turn record",
            "run `tycho init` — one run adds the ignore rule and repairs anything else "
            "outdated above",
        ))
    return findings


def _upgrade_notes(repo: Path) -> list[Finding]:
    """Said once to an upgrader, and only here: an install still stamped with an older schema
    predates 0.2.0's silence, and "I updated and Tycho stopped working" is the ticket that
    release earns. `tycho init` clears the stamp, and with it this note."""
    stamped = state.installed_schema(repo)
    if stamped is None or stamped >= state.SCHEMA:
        return []
    return [Finding(
        INFO,
        "0.2.0 is quiet by design — where 0.1.0 printed a verdict after every turn, the hook "
        "now prints a digest only when a turn is unusual. Silence means it verified.",
        "`tycho show` for the last turn, `tycho log` for history",
    )]


def _heartbeat_finding(repo: Path, wired: list[str]) -> Finding:
    """The liveness answer, hedged as the module docstring requires."""
    beat = state.last_run(repo)
    at = beat.get("at") if beat else None
    if not isinstance(at, (int, float)):
        return Finding(
            INFO,
            "the hook has not run here yet — no heartbeat recorded",
            f"finish a turn in {'/'.join(wired)}, then re-run `tycho doctor`",
        )
    age = max(0.0, time.time() - at)
    return Finding(OK, f"hook last fired {_ago(age)} (via {beat.get('harness', '?')})")


def _transcript_finding(repo: Path) -> Finding:
    """A wired, runnable hook still verifies nothing without a readable session."""
    path, harness = harness_mod.discover(repo)
    if path is None:
        return Finding(INFO, "no agent session found for this repo yet — nothing to verify")
    try:
        return Finding(OK, f"most recent session: {harness.name} ({path})")
    finally:
        if harness.name == "opencode":
            path.unlink(missing_ok=True)  # a rebuilt temp file — discovery hands it over


def _harness_drift(wired: list[str]) -> list[Finding]:
    """Has a wired harness moved past the version its hook contract was verified against?

    Offline `--version` probe — the best available proxy for "does the harness still read
    our output". Fails open: no pinned contract or an unreadable `--version` stays silent."""
    findings: list[Finding] = []
    for name in wired:
        pinned = harness_mod.VERIFIED_AGAINST.get(name)
        if not pinned:
            continue
        installed = _probe_version(pinned["probe"])
        if installed is None or pinned["version"] in installed:
            continue
        findings.append(Finding(
            DRIFT,
            f"{name}: hook contract verified against {pinned['version']}, you have {installed}",
            "re-verify Tycho's output fields against this version — a harness can change its "
            "hook contract in a patch release, and the hook would still fire while the verdict "
            "reached nobody",
        ))
    return findings


def _probe_version(probe: tuple[str, ...]) -> str | None:
    """The harness's own `--version`, first line, or None. Never raises: a missing binary,
    timeout or non-zero exit is "can't tell", i.e. silence."""
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    return lines[0].strip() if lines else None


def healthy(findings: list[Finding]) -> bool:
    return not adverse(findings)


def render(findings: list[Finding]) -> str:
    # `diagnose` already ran the forced check, so the cache now holds the true latest.
    latest = version_mod.cached_latest()
    if latest and version_mod.is_newer(latest, __version__):
        head = f"tycho doctor (v{__version__} · latest {latest} — run `tycho update`)"
    elif latest:
        head = f"tycho doctor (v{__version__} · latest {latest})"
    else:
        head = f"tycho doctor (v{__version__})"  # index unreachable / opted out
    lines = [head]
    for f in findings:
        mark = {OK: "✓", INFO: "•", DRIFT: "⚠", EXPOSED: "⚠"}.get(f.level, "✗")
        label = "" if f.level in (OK, INFO, DRIFT) else f"{f.level}: "
        lines.append(f"  {mark} {label}{f.text}")
        if f.fix:
            lines.append(f"      → {f.fix}")
    lines.append("")
    lines.append("  healthy" if healthy(findings) else "  NOT healthy — Tycho is not verifying this repo")
    return "\n".join(lines)

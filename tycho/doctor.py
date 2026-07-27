"""`tycho doctor` — is Tycho actually wired, current, and still firing here?

A silently dead hook is the worst failure a verifier has. Every other bug is loud: a
wrong verdict argues with you. A dead hook says nothing at all, and silence is exactly
what "everything passed" looks like — you believe you're covered while nothing runs.
(the OpenCode export truncation, was this class of failure.)

**The hard part is that a dead hook cannot report its own death.** Nothing Tycho does at
Stop time can help, because a hook that doesn't fire runs no code. So liveness can only
come from something the hook *already did*: `state.record_run` writes a heartbeat on
every invocation, and `doctor` — a manual command, run by a human who is present — reads
it back. That's the whole trick, and its limits are worth stating plainly:

- A heartbeat proves the hook fired at least once, at that time. Proof of life, never
  proof of current health.
- **No heartbeat is not proof of death.** A fresh install hasn't fired yet; neither has
  a repo you haven't run an agent in. Reporting BROKEN there would be crying wolf, and a
  diagnostic that cries wolf gets ignored exactly when it's finally right.
- Nothing here polls or runs in the background — Tycho stays a thing that runs when
  called. The cost is that a hook which died five minutes ago goes undiagnosed until
  someone asks. That's the honest trade, documented rather than hidden.

So doctor reports what it can prove from disk and says "unknown" where it can't. It
never edits anything: diagnosis and repair stay separate, and `tycho init` is already
the repair (it's self-healing).
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
from . import harness as harness_mod
from . import init as init_mod
from . import state
from . import version as version_mod
from .review import _ago

# Severities. BROKEN and OUTDATED are the two that mean "Tycho is not doing its job";
# the rest is context for whoever is reading.
OK = "OK"
BROKEN = "HOOK BROKEN"
OUTDATED = "HOOK OUTDATED"
INFO = "INFO"
# Advisory only: the harness moved past the version Tycho's output contract was verified
# against. "Re-verify", not "broken" — a bump usually changes nothing, so it never sinks
# the verdict (not in _ADVERSE). But louder than a bare INFO bullet, because it's the one
# thing doctor structurally can't check for itself: whether the harness still reads our
# output.
DRIFT = "HARNESS DRIFT"

_ADVERSE = (BROKEN, OUTDATED)


@dataclass(frozen=True)
class Finding:
    """One diagnosis.

    `fix` is mandatory for adverse levels: a complaint without a remedy is noise, and
    the user is reading this *because* something is off.
    """

    level: str
    text: str
    fix: str = ""


def hook_health(repo: Path) -> list[Finding]:
    """Only the adverse wiring findings: is what's installed runnable, and current?

    No discovery, no heartbeat — cheap enough to run on *every* manual command. That's
    the point: a broken hook has to be surfaced by the command people actually run, not
    only by the one they'd run if they already suspected something was wrong.
    """
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
    # A new version is worth surfacing where someone is already looking; doctor is manual,
    # so the (once-a-day, fail-open) network check is fine here.
    note = version_mod.notice(refresh_first=True, force=True)  # explicit command — bypass the daily cache
    if note:
        findings.append(Finding(INFO, note, "run `tycho update`"))
    wired = _wired_harnesses(repo, findings)

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
    """One line answering "is it on here?" — the payoff line of `tycho help`.

    The same reads `diagnose` does, collapsed to a sentence and never raised: help is the
    command a confused user reaches for, so it degrades to an honest "unknown" rather than
    dying on a repo where nothing is installed. `tycho doctor` stays the full answer.
    """
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
    """Diagnose one harness against its *own config* — the only thing that decides
    whether Tycho fires. install.json is a claim; the harness's config is the truth."""
    try:
        command = init_mod.installed_command(repo, name)
    except init_mod.ConfigRefused as exc:
        findings.append(Finding(BROKEN, f"{name}: {exc}", "fix that file, then re-run `tycho init`"))
        return False

    if command is None:
        if name in recorded:
            # We installed it and it's gone: an upgrade rewrote the config, someone
            # hand-edited it, or a teammate's settings landed on top. Loud, because this
            # is precisely the case where the user believes they're covered and isn't.
            findings.append(Finding(
                BROKEN,
                f"{name}: Tycho installed a hook here, but it's gone from {init_mod.config_path(repo, name)}",
                "run `tycho init` to reinstall it",
            ))
        return False

    if not _resolves(command):
        # The entry is there, so the harness dutifully runs it — into a missing
        # interpreter. The config looks installed and nothing has ever run: the exact
        # silent death this command exists to surface.
        findings.append(Finding(
            BROKEN,
            f"{name}: hook command doesn't resolve to a runnable program — {command}",
            "run `tycho init` to rewrite it for this environment",
        ))
        return True  # wired (the entry exists), just not working

    # Deliberately *not* compared against `init.hook_command()`. That returns a console
    # script or `<python> -m` depending on whether the venv happens to be on PATH right
    # now, so the same healthy install reads as two different "current" answers — and
    # flagging a hook that resolves and runs would be crying wolf (see module docstring).
    # A command that resolves and is one of ours does the job; that's the bar. A stale
    # path to a deleted venv doesn't resolve, and is caught above.
    findings.append(Finding(OK, f"{name}: hook installed and runnable — {command}"))
    return True


def _resolves(command: str) -> bool:
    """Would the host shell find something to execute here?

    The hook fires without a venv, so this is the question that actually matters — and
    the one a config-shape check can't answer. Split the way the *host* shell would
    (Windows keeps backslashes; POSIX treats them as escapes), then: a path must exist
    on disk, a bare name must be on PATH. The executable bit is a POSIX concept — on
    Windows, existence (with ``which`` honouring PATHEXT) is the runnable test.
    """
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


def _heartbeat_finding(repo: Path, wired: list[str]) -> Finding:
    """The liveness answer, with the hedging the module docstring argues for."""
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

    Offline `--version` probe per harness. Doctor proves the hook *fires*; it can't prove
    the harness still *reads* our output — version drift is the best available proxy for
    that blind spot. Fails open in both directions: a harness with no pinned contract, a
    missing/unparseable `--version`, or a matching version all stay silent — only a real
    mismatch speaks, and it says "re-verify", not "broken".
    """
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
            "re-verify Tycho's output fields against this version — see docs/harness-support.md",
        ))
    return findings


def _probe_version(probe: tuple[str, ...]) -> str | None:
    """The harness's own `--version`, first line — or None if it can't be read.

    Never raises: a missing binary, a timeout, or a non-zero exit is "can't tell", which
    doctor treats as silence. Same fail-open rule as the Stop hook."""
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
        mark = {OK: "✓", INFO: "•", DRIFT: "⚠"}.get(f.level, "✗")
        label = "" if f.level in (OK, INFO, DRIFT) else f"{f.level}: "
        lines.append(f"  {mark} {label}{f.text}")
        if f.fix:
            lines.append(f"      → {f.fix}")
    lines.append("")
    lines.append("  healthy" if healthy(findings) else "  NOT healthy — Tycho is not verifying this repo")
    return "\n".join(lines)

"""Running a verification, and the setup noise around one.

`_verify` is the whole pipeline: discover a transcript, gather, run the checks, render, record.
The rest is what a manual run must say first — is Tycho installed here, and is its hook alive.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..model import CheckResult, CheckStatus, Verdict
from ..read import harness as harness_mod
from ..read import session as engine
from ..views.report import render
from . import ExitCode

_VERDICT_EXIT = {Verdict.FAILED: ExitCode.FAILED, Verdict.STALE: ExitCode.STALE}

# One line per command, defined once — both `-h` and `tycho help` render these, so they


def _install(lines: Sequence[str]) -> int:
    """Print init/uninstall status lines; exit non-zero if we refused to touch a file — a
    refusal is an unfinished install, and `tycho init --yes` in CI must fail loudly."""
    from ..wire.install import REFUSED

    for line in lines:
        print(line)
    return ExitCode.INTERNAL if any(REFUSED in line for line in lines) else ExitCode.OK


def _warn_if_hook_broken(cwd: Path) -> None:
    """Say so, loudly, if Tycho is installed here but wouldn't actually fire — a verdict looks
    identical whether the Stop hook has been running all week or dead since the venv moved.
    Stderr so it can't pollute a piped report, and never fatal."""
    try:
        from ..wire import doctor

        for f in doctor.hook_health(cwd):
            print(f"tycho: {f.level} — {f.text}", file=sys.stderr)
            if f.fix:
                print(f"       → {f.fix}", file=sys.stderr)
    except Exception:
        pass  # a diagnostic must never be the reason a verify fails


def _offer_first_run(cwd: Path) -> None:
    """First-run 'set up Tycho here?' offer, printed for the manual commands. Never fatal."""
    try:
        from ..wire import install as init_mod

        for line in init_mod.offer_first_run(cwd):
            print(line)
    except Exception:
        pass


def _verify(args: argparse.Namespace) -> int:
    from ..store import state

    # The repo root, not wherever the user stands: harnesses store transcripts under the
    # *project* path, so discovery from a subdirectory would find no session.
    cwd = state.root_for(Path.cwd())
    _offer_first_run(cwd)
    _warn_if_hook_broken(cwd)
    if args.session:
        harness = harness_mod.BY_NAME.get(args.harness or "claude", harness_mod.CLAUDE)
        transcript = args.session
    else:
        transcript, harness = harness_mod.discover(cwd, only=args.harness)
        if transcript is None:
            note = CheckResult(
                "session",
                CheckStatus.INDETERMINATE,
                "no recent session found for this directory — pass --session <path>",
            )
            print(render(Verdict.INDETERMINATE, [note], claim=args.claim))
            return ExitCode.OK
        print(f"tycho: verifying {harness.name} session {transcript}")

    try:
        try:
            session = engine.gather(
                transcript, cwd, since=args.since or "HEAD",
                parse=harness.parse, messages=harness.messages,
            )
        finally:
            # A rebuilt transcript is a temp file discovery owns; one the harness maintains
            # must never be deleted. Declared, so a new harness needs no edit here.
            if not args.session and not harness.capabilities.transcript_is_file:
                transcript.unlink(missing_ok=True)
    except Exception as exc:
        # A traceback is not a verdict: say plainly that we couldn't verify.
        print(f"tycho: could not verify {transcript} — {type(exc).__name__}: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL

    results = engine.run_checks(session)
    verdict = engine.verdict_of(results)
    # A manual verify is a real verification event — record it so the status bar reflects it.
    try:
        state.record_run(cwd, harness.name, verdict=verdict.name)
        state.record_catch(cwd, harness.name, verdict.name, results)  # evidence trail
    except Exception:
        pass  # a status-bar convenience must never be why verify fails
    print(render(verdict, results, claim=args.claim))
    return _VERDICT_EXIT.get(verdict, ExitCode.OK)

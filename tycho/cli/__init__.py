"""`tycho` command-line entry point.

Exit codes are a public contract (CI gates depend on them): only an adverse finding is
non-zero, and each adverse kind gets its own code so a gate can choose what blocks.

`main` parses and dispatches; the commands live one module down:

    parser     what the CLI advertises — the argparse surface and `tycho help`
    verify     run the checks against a session, and the setup noise around one
    record     read the record back: show / blame / log / review / attest / count
    settings   the repo-local toggles: scope, relay, override
    update     is a newer Tycho out, and how would this install get it

The re-exports at the bottom are why `cli.X` still means what it always did — including for
the tests that monkeypatch through this module.
"""

from __future__ import annotations

import argparse
import os as os  # facade: `cli.os.path` and `cli.sys` are patched in tests
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path


class ExitCode(IntEnum):
    """Stable process exit codes. Don't renumber — CI gates depend on these."""

    OK = 0  # VERIFIED / UNSUPPORTED / INDETERMINATE — nothing adverse found
    FAILED = 1  # a check proved the claim wrong
    USAGE = 2  # bad invocation (argparse's own convention)
    STALE = 3  # edits landed after the last passing test run
    INTERNAL = 4  # Tycho itself could not complete (bad transcript/config/git)
    UNHEALTHY = 5  # `doctor`: Tycho is installed here but not working (broken/outdated)
    # `review --exit-code` only, never FAILED: a hunk nothing exercised is a coverage claim,
    # not proof anything is wrong.
    UNEXERCISED = 6
    MISMATCH = 7  # `attest --verify`: the trailer does not match the record


def _force_utf8() -> None:
    """Pin UTF-8 on all three streams, whatever the platform's locale says.

    stdout/stderr keep Windows' legacy cp1252 console from raising UnicodeEncodeError on our
    ✓/✗/•/→ glyphs. **stdin matters more**: the harness writes UTF-8 JSON, and a Windows
    reader defaulting to cp1252 either mojibakes a path (`C:\\Users\\Müller` → `MÃ¼ller`, so
    the hook fabricates a directory tree and verifies a repo that doesn't exist, while still
    beating a healthy heartbeat) or raises on a CJK path — and a raise out of the Stop hook
    breaks the agent's turn, which is the one thing this program must never do.

    errors=replace so a stream that genuinely can't do UTF-8 degrades instead of raising.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # fail open: cosmetic setup must never be why a command fails


def _count_arg(text: str) -> int:
    """A `-n` that must ask for at least one row. `-n 0` and `-n -5` are slices that read as
    "nothing matched" — the same output as a file no turn ever touched, which is a lie."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8()
    parser = parser_mod.build()

    args = parser.parse_args(argv)
    if args.command is None:  # bare `tycho` → verify the most recent session here
        return _verify(argparse.Namespace(session=None, harness=None, since=None, claim=None))
    if args.command == "help":
        return _help(Path.cwd())
    if args.command == "count":
        return _count(Path.cwd(), show_ledger=args.ledger)
    if args.command == "run":
        return _run(args.cmd)
    if args.command == "exec":
        from ..store import command as command_mod
        from ..store import state

        return command_mod.execute(state.root_for(Path.cwd()), args.cmd)
    if args.command == "show":
        return _show(Path.cwd(), args.turn, share=args.share)
    if args.command == "blame":
        return _archaeology("blame", Path.cwd(), args.target, args.limit)
    if args.command == "log":
        return _archaeology("log", Path.cwd(), None, args.limit,
                            verdict=args.verdict, since=args.since)
    if args.command == "review":
        return _review(Path.cwd(), args.since, exit_code=args.exit_code)
    if args.command == "backfill":
        return _backfill(Path.cwd(), limit=args.limit, dry_run=args.dry_run)
    if args.command == "attest":
        return _attest(Path.cwd(), verify=args.verify, write=args.write,
                       require_verified=args.require_verified)
    if args.command == "scope":
        return _scope(Path.cwd(), args.action, args.paths, args.exclude)
    if args.command == "update":
        return _update(skip=args.skip, force=args.force)
    if args.command == "relay":
        return _relay(Path.cwd(), on=args.on, off=args.off)
    if args.command == "rewrite":
        return _rewrite(Path.cwd(), on=args.on, off=args.off)
    if args.command == "override":
        return _override(Path.cwd(), check=args.check, reason=args.reason,
                         on=args.on, off=args.off, veto=args.veto, unveto=args.unveto)
    if args.command in ("statusline", "status"):
        from ..wire import status

        return status.main(off=args.off, on=args.on)
    if args.command == "hook":
        from ..wire import hook

        return hook.main()
    if args.command == "session-start":
        from ..wire import hook

        return hook.session_start()
    if args.command == "prompt-submit":
        from ..wire import hook

        return hook.prompt_submit()
    if args.command == "pre-tool-use":
        from ..wire import hook

        return hook.pre_tool_use()
    if args.command == "init":
        from ..wire import install as init_mod

        rc = _install(
            init_mod.init_global(assume_yes=args.yes) if args.globally
            else init_mod.init(Path.cwd(), only=args.harness, assume_yes=args.yes)
        )
        _print_update_notice()  # tell them if a newer Tycho exists
        return rc
    if args.command == "doctor":
        from ..wire import doctor

        _offer_first_run(Path.cwd())
        findings = doctor.diagnose(Path.cwd())
        print(doctor.render(findings))
        return ExitCode.OK if doctor.healthy(findings) else ExitCode.UNHEALTHY
    if args.command == "uninstall":
        from ..wire import install as init_mod

        return _install(
            init_mod.uninstall_global() if args.globally
            else init_mod.uninstall(Path.cwd(), only=args.harness, purge=args.purge)
        )
    if args.command == "verify":
        return _verify(args)
    parser.error(f"unknown command: {args.command}")  # argparse exits; unreachable below
    return ExitCode.USAGE


def _run(argv: list[str]) -> int:
    """`tycho run [--] <cmd>` — exec <cmd> and forward its output and exit code unchanged,
    so `command_execution` sees the true status however the command is wrapped or piped."""
    import subprocess

    cmd = argv[1:] if argv and argv[0] == "--" else argv
    if not cmd:
        print("tycho run: give a command, e.g. tycho run -- pytest -q", file=sys.stderr)
        return ExitCode.USAGE
    from ..store.command import launchable  # npm/yarn/npx are .cmd shims on Windows

    try:
        return subprocess.call(launchable(cmd))  # inherits stdio; child's real exit code
    except FileNotFoundError:
        print(f"tycho run: command not found: {cmd[0]}", file=sys.stderr)
        return ExitCode.USAGE
    except KeyboardInterrupt:
        return 130  # conventional 128+SIGINT, matching what a bare run would return


# Callers and tests name `cli.X`; where X lives is this package's business. The dispatch above
# resolves through these globals too, so a monkeypatched `cli.X` is still what main calls.
from . import parser as parser_mod  # noqa: E402
from .parser import _ABOUT as _ABOUT, _COMMANDS as _COMMANDS, _help as _help  # noqa: E402
from .record import (  # noqa: E402
    _archaeology as _archaeology,
    _attest as _attest,
    _backfill as _backfill,
    _caught as _caught,
    _count as _count,
    _ledger_lines as _ledger_lines,
    _model_label as _model_label,
    _pct as _pct,
    _rate as _rate,
    _review as _review,
    _show as _show,
)
from .settings import (  # noqa: E402
    _override as _override,
    _relay as _relay,
    _rewrite as _rewrite,
    _scope as _scope,
)
from .update import (  # noqa: E402
    _is_homebrew_install as _is_homebrew_install,
    _print_update_notice as _print_update_notice,
    _spawn_deferred_upgrade as _spawn_deferred_upgrade,
    _update as _update,
    _upgrade_command as _upgrade_command,
)
from .verify import (  # noqa: E402
    _install as _install,
    _offer_first_run as _offer_first_run,
    _verify as _verify,
    _warn_if_hook_broken as _warn_if_hook_broken,
    harness_mod as harness_mod,
)

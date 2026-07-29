"""What the CLI advertises: the argparse surface, and the one screen `tycho help` prints."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import __version__
from ..read import harness as harness_mod
from . import ExitCode, _count_arg

# One line per command, defined once — both `-h` and `tycho help` render these, so they
# can't drift apart.
_COMMANDS = {
    "verify": "verify what the agent claimed and render a verdict",
    "hook": "Stop-hook entrypoint: read hook JSON on stdin, verify, print",
    "init": "install Tycho's hook into this repo's detected harnesses",
    "doctor": "check that Tycho's hooks are installed, current, and firing",
    "uninstall": "remove Tycho's hooks (leaves your other hooks alone)",
    "statusline": "one line for a harness status bar: is Tycho live here, and the last verdict",
    "count": "how many problems Tycho has caught — in this repo, and all-time",
    "show": "the full digest of a turn: what changed, what ran, what's still unverified",
    "blame": "which turn touched this file, what the agent claimed, and what backed it",
    "log": "the recorded history of what agents did in this repo, newest first",
    "review": "risk-focus the diff: which changes no test covered and no command exercised",
    "backfill": "seed the record from transcripts written before Tycho was installed",
    "attest": "print the Tycho-Attestation trailer for the latest recorded turn",
    "run": "run a command so its true exit code is seen even when wrapped/piped: tycho run -- pytest",
    "exec": "run a command and put its real output and exit status on the record",
    "scope": "show or edit which files the agent may edit (the scope_drift allowlist)",
    "relay": "let Claude/Codex see its verdict and keep working until VERIFIED (bounded, off by default)",
    "override": "record a per-check verdict override when the relay is on (agent-authorized, logged, off by default)",
    "rewrite": "route a piped test runner through `tycho exec` so its real exit status is recorded (off by default)",
    "update": "check for and install a newer Tycho",
    "help": "what Tycho is, whether it's live here, and every command",
}


_ABOUT = """\
Tycho proves an agent did what it claimed. It runs as a Stop hook: when a turn ends it
reads git, the filesystem, exit codes, and the harness's own event stream, then prints a
verdict — VERIFIED, FAILED, or STALE. Entirely offline and stdlib-only: no account, no
network, no LLM in the trust path. It never blocks your agent; only `tycho verify` exits
non-zero, so CI can gate on it."""


def build() -> argparse.ArgumentParser:
    """The whole `tycho` argparse surface."""
    parser = argparse.ArgumentParser(
        prog="tycho",
        description="Verify what an agent claims it did.",
        epilog="run `tycho help` for what Tycho is and whether it's live in this repo",
    )
    parser.add_argument("--version", action="version", version=f"tycho {__version__}")
    # Not required: bare `tycho` defaults to `verify`, the on-demand path that works even
    # where the Stop hook can't fire (e.g. Codex on Windows).
    sub = parser.add_subparsers(dest="command", required=False)

    v = sub.add_parser("verify", help=_COMMANDS["verify"])
    v.add_argument("--session", type=Path, help="path to a harness transcript to verify")
    v.add_argument(
        "--harness",
        choices=harness_mod.ENABLED_NAMES,
        help="which harness to verify (default: whichever ran most recently)",
    )
    v.add_argument("--since", help="git ref to diff from (manual mode), e.g. HEAD~1")
    v.add_argument("--claim", help="free-text claim being checked (shown in the report)")

    sub.add_parser("hook", help=_COMMANDS["hook"])
    sub.add_parser("session-start", help="SessionStart-hook entrypoint (internal): update notice at agent bootup")
    sub.add_parser("prompt-submit", help="UserPromptSubmit-hook entrypoint (internal): mark a run in flight for the badge")
    sub.add_parser("pre-tool-use", help="PreToolUse-hook entrypoint (internal): keep a piped runner's exit status readable")
    i = sub.add_parser("init", help=_COMMANDS["init"])
    i.add_argument(
        "--harness",
        choices=harness_mod.ENABLED_NAMES,
        help="install only this harness, detected or not (default: every one detected here)",
    )
    i.add_argument("--yes", action="store_true", help="skip the prompts (for scripts and CI)")
    i.add_argument(
        "--global", dest="globally", action="store_true",
        help="install for EVERY repo on this machine (opt-in, defers to any per-repo install)",
    )
    sub.add_parser("doctor", help=_COMMANDS["doctor"])
    u = sub.add_parser("uninstall", help=_COMMANDS["uninstall"])
    u.add_argument(
        "--harness",
        choices=("claude", "cursor", "codex", "opencode"),
        help="only uninstall this harness (default: all)",
    )
    u.add_argument(
        "--purge",
        action="store_true",
        help="also delete repo-local Tycho state (.tycho/) and config (.tycho.toml)",
    )
    u.add_argument(
        "--global", dest="globally", action="store_true",
        help="remove the machine-wide install instead of this repo's",
    )
    # `status` stays as a hidden back-compat alias: deployed statusLine entries and slash
    # commands still say `status`.
    s = sub.add_parser("statusline", aliases=["status"], help=_COMMANDS["statusline"])
    toggle = s.add_mutually_exclusive_group()
    toggle.add_argument("--off", action="store_true", help="hide the indicator in this repo (the hook keeps verifying)")
    toggle.add_argument("--on", action="store_true", help="show the indicator again")
    ct = sub.add_parser("count", help=_COMMANDS["count"])
    ct.add_argument(
        "--ledger", action="store_true",
        help="the decay ledger: per-model and per-check catch and blind rates",
    )
    sh = sub.add_parser("show", help=_COMMANDS["show"])
    sh.add_argument("turn", nargs="?", metavar="TURN",
                    help="a turn id from `tycho log` (default: the most recent turn)")
    bl = sub.add_parser("blame", help=_COMMANDS["blame"])
    bl.add_argument("target", metavar="PATH[:LINE]", help="the file (optionally :line) to blame")
    bl.add_argument("-n", "--limit", type=_count_arg, default=10, help="how many turns to show (default 10)")
    lg = sub.add_parser("log", help=_COMMANDS["log"])
    lg.add_argument("-n", "--limit", type=_count_arg, default=20, help="how many turns to show (default 20)")
    # Both filter *inside* the bounded stream: `--verdict FAILED -n 20` gives twenty failures,
    # not the failures among the last twenty turns.
    lg.add_argument("--verdict", help="only turns with this verdict, e.g. FAILED")
    lg.add_argument("--since", metavar="YYYY-MM-DD", help="only turns on or after this date")
    rv = sub.add_parser("review", help=_COMMANDS["review"])
    rv.add_argument("--since", default="HEAD", help="git ref to diff from (default: HEAD)")
    # Gates on UNEXERCISED/NO TEST RUN only. Never UNRECORDED — that just means no turn
    # Tycho saw touched the file, and gating on it would fail every honest commit.
    rv.add_argument(
        "--exit-code", action="store_true",
        help=f"exit {int(ExitCode.UNEXERCISED)} if a recorded change had no command run after "
             f"it (default: always 0 — review is advisory)",
    )
    bf = sub.add_parser("backfill", help=_COMMANDS["backfill"])
    bf.add_argument("-n", "--limit", type=_count_arg,
                    help="at most this many turns, newest first (default: what the record retains)")
    bf.add_argument("--dry-run", action="store_true",
                    help="say how many turns are available without writing any")
    at = sub.add_parser("attest", help=_COMMANDS["attest"])
    at_mode = at.add_mutually_exclusive_group()
    at_mode.add_argument("--verify", nargs="?", const="HEAD", metavar="REF",
                         help="check a commit's trailer against the record (default: HEAD)")
    at_mode.add_argument("--write", nargs="+", metavar=("MSGFILE", "SOURCE"),
                         help="prepare-commit-msg entrypoint (internal): add the trailer to MSGFILE")
    # A matching trailer says the record is intact, not that the work was proven — a commit
    # whose every turn came back FAILED matches its own record perfectly.
    at.add_argument("--require-verified", action="store_true",
                    help="with --verify: also require every covered turn to have reached VERIFIED")
    r = sub.add_parser("run", help=_COMMANDS["run"])
    r.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="the command to run, e.g. tycho run -- pytest -q (the -- is optional)",
    )
    ex = sub.add_parser("exec", help=_COMMANDS["exec"])
    ex.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="the command to run and record, e.g. tycho exec -- pytest -q (the -- is optional)",
    )
    sc = sub.add_parser("scope", help=_COMMANDS["scope"])
    sc.add_argument("action", choices=("list", "set", "add", "remove"), help="list, or set/add/remove globs")
    sc.add_argument(
        "paths", nargs="*", metavar="GLOB",
        help="one or more include globs — quote them so the shell keeps them literal, e.g. 'src/**' 'tests/**'",
    )
    # nargs="+", not store_true: a bare flag between the `action` and `paths` positionals
    # splits them into two groups, and argparse < 3.12 drops the trailing glob.
    sc.add_argument(
        "--exclude", nargs="+", metavar="GLOB",
        help="operate on the exclude list (paths carved OUT of include) instead, e.g. --exclude 'LICENSE'",
    )
    rl = sub.add_parser("relay", help=_COMMANDS["relay"])
    rl_toggle = rl.add_mutually_exclusive_group()
    rl_toggle.add_argument(
        "--on", action="store_true",
        help="feed a non-VERIFIED verdict back to the agent so it keeps working until VERIFIED (bounded)",
    )
    rl_toggle.add_argument(
        "--off", action="store_true", help="stop feeding the verdict to the agent (the default)",
    )
    rw = sub.add_parser("rewrite", help=_COMMANDS["rewrite"])
    rw_toggle = rw.add_mutually_exclusive_group()
    rw_toggle.add_argument(
        "--on", action="store_true",
        help="route a piped runner through `tycho exec` so its real exit status is recorded",
    )
    rw_toggle.add_argument(
        "--off", action="store_true", help="leave commands exactly as the agent wrote them (the default)",
    )
    ov = sub.add_parser("override", help=_COMMANDS["override"])
    ov.add_argument("check", nargs="?", metavar="CHECK",
                    help="the check to override (e.g. test_freshness); omit to show status")
    ov.add_argument("reason", nargs="?", metavar="REASON",
                    help="why the check does not apply — required, logged, shown to the user")
    ov_toggle = ov.add_mutually_exclusive_group()
    ov_toggle.add_argument("--on", action="store_true",
                           help="allow the agent to record verdict overrides here")
    ov_toggle.add_argument("--off", action="store_true",
                           help="disallow overrides (the default)")
    ov_toggle.add_argument("--veto", action="store_true",
                           help="countermand an override so the relay fires again (name a CHECK, or bare to veto all active)")
    ov_toggle.add_argument("--unveto", action="store_true",
                           help="lift a previous veto so CHECK may be overridden again")
    up = sub.add_parser("update", help=_COMMANDS["update"])
    up.add_argument("--skip", action="store_true", help="dismiss the current update notice (records the dismissal)")
    up.add_argument("--force", action="store_true", help="reinstall the latest even across a pinned version (uv/pipx)")
    sub.add_parser("help", help=_COMMANDS["help"])
    return parser


def _help(cwd: Path) -> int:
    """One screen: what Tycho is, whether it's live *here*, and every command. The liveness
    line is the reason this exists — `-h` can't answer "is it on?"."""
    from ..wire import doctor

    print(f"tycho {__version__} — verify what an agent claims it did.")
    print(f"\n{_ABOUT}\n")
    print(f"Status here: {doctor.liveness(cwd)}\n")
    print("Commands:")
    for name, text in _COMMANDS.items():
        print(f"  {name:<10} {text}")
    print("\n  `tycho doctor` for full diagnostics · https://github.com/swail-labs/tycho")
    return ExitCode.OK

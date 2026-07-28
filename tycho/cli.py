"""`tycho` command-line entry point.

Exit codes are a public contract (CI gates depend on them): only an adverse finding is
non-zero, and each adverse kind gets its own code so a gate can choose what blocks.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path

from . import __version__
from . import harness as harness_mod
from . import verify as engine
from .model import CheckResult, CheckStatus, Verdict
from .report import render


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


_VERDICT_EXIT = {Verdict.FAILED: ExitCode.FAILED, Verdict.STALE: ExitCode.STALE}

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
    "attest": "print the Tycho-Attestation trailer for the latest recorded turn",
    "run": "run a command so its true exit code is seen even when wrapped/piped: tycho run -- pytest",
    "exec": "run a command and put its real output and exit status on the record",
    "scope": "show or edit which files the agent may edit (the scope_drift allowlist)",
    "relay": "let Claude/Codex see its verdict and keep working until VERIFIED (bounded, off by default)",
    "override": "record a per-check verdict override when the relay is on (agent-authorized, logged, off by default)",
    "update": "check for and install a newer Tycho",
    "help": "what Tycho is, whether it's live here, and every command",
}

_ABOUT = """\
Tycho proves an agent did what it claimed. It runs as a Stop hook: when a turn ends it
reads git, the filesystem, exit codes, and the harness's own event stream, then prints a
verdict — VERIFIED, FAILED, or STALE. Entirely offline and stdlib-only: no account, no
network, no LLM in the trust path. It never blocks your agent; only `tycho verify` exits
non-zero, so CI can gate on it."""


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


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8()
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
    bl.add_argument("-n", "--limit", type=int, default=10, help="how many turns to show (default 10)")
    lg = sub.add_parser("log", help=_COMMANDS["log"])
    lg.add_argument("-n", "--limit", type=int, default=20, help="how many turns to show (default 20)")
    # Both filter *inside* the bounded stream: `--verdict FAILED -n 20` gives twenty failures,
    # not the failures among the last twenty turns.
    lg.add_argument("--verdict", help="only turns with this verdict, e.g. FAILED")
    lg.add_argument("--since", metavar="YYYY-MM-DD", help="only turns on or after this date")
    rv = sub.add_parser("review", help=_COMMANDS["review"])
    rv.add_argument("--since", default="HEAD", help="git ref to diff from (default: HEAD)")
    # Advisory by default; this is the opt-in gate. Gates on UNEXERCISED/NO TEST RUN only —
    # never on UNRECORDED, which just means no turn Tycho saw touched the file (hand-written,
    # or predating the install), and gating on that would fail every honest commit.
    rv.add_argument(
        "--exit-code", action="store_true",
        help=f"exit {int(ExitCode.UNEXERCISED)} if a recorded change had no command run after "
             f"it (default: always 0 — review is advisory)",
    )
    at = sub.add_parser("attest", help=_COMMANDS["attest"])
    at_mode = at.add_mutually_exclusive_group()
    at_mode.add_argument("--verify", nargs="?", const="HEAD", metavar="REF",
                         help="check a commit's trailer against the record (default: HEAD)")
    at_mode.add_argument("--write", nargs="+", metavar=("MSGFILE", "SOURCE"),
                         help="prepare-commit-msg entrypoint (internal): add the trailer to MSGFILE")
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
        from . import command as command_mod
        from . import state

        return command_mod.execute(state.root_for(Path.cwd()), args.cmd)
    if args.command == "show":
        return _show(Path.cwd(), args.turn)
    if args.command == "blame":
        return _archaeology("blame", Path.cwd(), args.target, args.limit)
    if args.command == "log":
        return _archaeology("log", Path.cwd(), None, args.limit,
                            verdict=args.verdict, since=args.since)
    if args.command == "review":
        return _review(Path.cwd(), args.since, exit_code=args.exit_code)
    if args.command == "attest":
        return _attest(Path.cwd(), verify=args.verify, write=args.write)
    if args.command == "scope":
        return _scope(Path.cwd(), args.action, args.paths, args.exclude)
    if args.command == "update":
        return _update(skip=args.skip, force=args.force)
    if args.command == "relay":
        return _relay(Path.cwd(), on=args.on, off=args.off)
    if args.command == "override":
        return _override(Path.cwd(), check=args.check, reason=args.reason,
                         on=args.on, off=args.off, veto=args.veto, unveto=args.unveto)
    if args.command in ("statusline", "status"):
        from . import status

        return status.main(off=args.off, on=args.on)
    if args.command == "hook":
        from . import hook

        return hook.main()
    if args.command == "session-start":
        from . import hook

        return hook.session_start()
    if args.command == "prompt-submit":
        from . import hook

        return hook.prompt_submit()
    if args.command == "init":
        from . import init as init_mod

        rc = _install(
            init_mod.init_global(assume_yes=args.yes) if args.globally
            else init_mod.init(Path.cwd(), only=args.harness, assume_yes=args.yes)
        )
        _print_update_notice()  # tell them if a newer Tycho exists
        return rc
    if args.command == "doctor":
        from . import doctor

        _offer_first_run(Path.cwd())
        findings = doctor.diagnose(Path.cwd())
        print(doctor.render(findings))
        return ExitCode.OK if doctor.healthy(findings) else ExitCode.UNHEALTHY
    if args.command == "uninstall":
        from . import init as init_mod

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
    from .command import launchable  # npm/yarn/npx are .cmd shims on Windows

    try:
        return subprocess.call(launchable(cmd))  # inherits stdio; child's real exit code
    except FileNotFoundError:
        print(f"tycho run: command not found: {cmd[0]}", file=sys.stderr)
        return ExitCode.USAGE
    except KeyboardInterrupt:
        return 130  # conventional 128+SIGINT, matching what a bare run would return


def _show(cwd: Path, turn: str | None) -> int:
    """`tycho show [TURN]` — the full digest of a turn, on demand."""
    from . import digest as digest_mod
    from . import record as record_mod
    from . import state

    repo = state.root_for(cwd)
    records = record_mod.read(repo, limit=1) if not turn else [
        r for r in record_mod.read(repo) if r.get("id", "").startswith(turn)
    ]
    if not records:
        print("tycho: no turn recorded yet — the Stop hook writes one per verified turn.")
        return ExitCode.OK
    print(digest_mod.render(records[0]))
    return ExitCode.OK


def _archaeology(action: str, cwd: Path, target: str | None, limit: int,
                 verdict: str | None = None, since: str | None = None) -> int:
    """`tycho blame <path>` / `tycho log` — what the agent did here."""
    from . import archaeology
    from . import state

    repo = state.root_for(cwd)
    lines = (
        archaeology.blame(repo, target, limit) if action == "blame"
        else archaeology.log(repo, limit, verdict=verdict, since=since)
    )
    for line in lines:
        print(line)
    return ExitCode.OK


def _review(cwd: Path, since: str, exit_code: bool = False) -> int:
    """`tycho review` — which changes nothing exercised. Advisory unless `--exit-code`."""
    from . import review as review_mod
    from . import state

    lines, findings = review_mod.inspect(state.root_for(cwd), since)
    for line in lines:
        print(line)
    if not exit_code:
        return ExitCode.OK
    return ExitCode.UNEXERCISED if review_mod.unexercised(findings) else ExitCode.OK


def _attest(cwd: Path, verify: str | None = None, write: list[str] | None = None) -> int:
    """`tycho attest [--verify REF | --write MSGFILE [SOURCE]]`. Bare: print the trailer for
    what's staged. `--verify` exits MISMATCH only on a genuine mismatch, never on "cannot tell" —
    a pruned record must not read as a forged one. `--write` can never fail a commit."""
    from . import attest as attest_mod
    from . import state

    repo = state.root_for(cwd)
    if write:
        return attest_mod.main(write)
    if verify:
        ok, text = attest_mod.verify(repo, verify)
        print(text)
        return ExitCode.MISMATCH if ok is False else ExitCode.OK
    line = attest_mod.trailer(repo)
    if line is None:
        print("tycho: nothing staged that a recorded turn touched — nothing to attest.")
        return ExitCode.OK
    print(line)
    return ExitCode.OK


def _help(cwd: Path) -> int:
    """One screen: what Tycho is, whether it's live *here*, and every command. The liveness
    line is the reason this exists — `-h` can't answer "is it on?"."""
    from . import doctor

    print(f"tycho {__version__} — verify what an agent claims it did.")
    print(f"\n{_ABOUT}\n")
    print(f"Status here: {doctor.liveness(cwd)}\n")
    print("Commands:")
    for name, text in _COMMANDS.items():
        print(f"  {name:<10} {text}")
    print("\n  `tycho doctor` for full diagnostics · https://github.com/swail-labs/tycho")
    return ExitCode.OK


def _count(cwd: Path, show_ledger: bool = False) -> int:
    """`tycho count [--ledger]` — the running tally of what Tycho caught, read straight off what
    the hook wrote (no engine, no verification). "Caught" is FAILED + STALE; INDETERMINATE folds
    into *blind*, because a blind spot isn't a save."""
    from . import state

    totals = state.totals(cwd)
    here = _caught(state.counts(cwd), totals)
    everywhere = _caught(state.all_time_counts(), state.all_time_totals())
    print(f"this repo: {here} · all-time: {everywhere}")
    if show_ledger:
        print()
        # `ledger` resolves the root itself; `totals` is passed in so the view can explain
        # its denominator against the tally printed above it.
        for line in _ledger_lines(state.ledger(cwd), repo_runs=totals.get("runs")):
            print(line)
    return ExitCode.OK


def _caught(counts: dict, totals: dict) -> str:
    """"274 runs, 41 blind (15%), 12 caught (9 FAILED, 3 STALE)". Blind rate leads and shows
    even at 0%: it's the one number that doesn't improve as models get better."""
    caught = counts["FAILED"] + counts["STALE"]
    breakdown = ", ".join(f"{counts[v]} {v}" for v in ("FAILED", "STALE") if counts[v])
    text = f"{caught} caught ({breakdown})" if caught else "0 caught"
    runs = totals["runs"]
    if not runs:  # legacy tally with no denominator, or a genuinely quiet repo
        return text
    blind = totals["blind"]  # INDETERMINATE + UNSUPPORTED — runs Tycho couldn't speak to
    return (f"{runs} run{'' if runs == 1 else 's'}, "
            f"{blind} blind ({round(100 * blind / runs)}%), {text}")


def _pct(n: int, denominator: int) -> str:
    """"25%", or "—" when the denominator is zero — an empty denominator is not 0%."""
    return f"{round(100 * n / denominator)}%" if denominator else "—"


def _rate(n: int, denominator: int) -> str:
    return f"{n} ({_pct(n, denominator)})"


def _ledger_lines(data: dict, repo_runs: int | None = None) -> list[str]:
    """Render `state.ledger` — per-model and per-check catch/blind rates. Per check, catch rate
    is over the turns the check could *speak* to, blind rate over every turn it ran in."""
    import time as time_mod

    turns = data["turns"]
    if not turns:
        return ["ledger: no turns recorded here yet — the Stop hook writes one per verified "
                "turn to .tycho/turns.jsonl."]
    span = " ".join(
        time_mod.strftime("%Y-%m-%d", time_mod.localtime(ts))
        for ts in (data["first"], data["last"]) if ts
    ).split()
    when = f", {span[0]} → {span[-1]}" if span else ""
    out = [
        f"ledger: {turns} turn{'' if turns == 1 else 's'} on the record{when}, "
        f"{data['blind']} blind ({_pct(data['blind'], turns)}), "
        f"{data['caught']} caught ({_pct(data['caught'], turns)})",
        "  (the retained turn record — `count` above is the all-time tally)",
    ]
    # `count` and the ledger legitimately differ; name why. Do NOT close the gap by recording a
    # turn from `tycho verify` — it audits a whole session, so it would invent a turn boundary.
    if repo_runs is not None and repo_runs > turns:
        from .record import max_records

        out.append(
            f"  ({repo_runs - turns} of this repo's {repo_runs} runs aren't turns here: "
            f"`tycho verify` audits a whole session, and the record keeps the last "
            f"{max_records()})"
        )
    out.append("")
    width = max([24, *(len(_model_label(m)) + 2 for m in data["models"])])
    out.append(f"  {'model':<{width}}{'turns':>6}  {'caught':<11}{'blind':<11}")
    for m in data["models"]:
        out.append(f"  {_model_label(m):<{width}}{m['turns']:>6}  "
                   f"{_rate(m['caught'], m['turns']):<11}{_rate(m['blind'], m['turns']):<11}")
    if not data["checks"]:
        return [line.rstrip() for line in out]
    cwidth = max([24, *(len(c["name"]) + 2 for c in data["checks"])])
    out += ["", f"  {'check':<{cwidth}}{'spoke':>6}  {'caught':<11}{'blind':<11}by model (caught/spoke)"]
    for c in data["checks"]:
        by_model = ", ".join(
            f"{m['model'] or 'unknown model'} {m['caught']}/{m['spoke']}" for m in c["models"]
        )
        out.append(f"  {c['name']:<{cwidth}}{c['spoke']:>6}  "
                   f"{_rate(c['caught'], c['spoke']):<11}"
                   f"{_rate(c['blind'], c['spoke'] + c['blind']):<11}{by_model}")
    out += ["", "  catch rate = caught / turns the check could speak to (PASS|FAIL|STALE).",
            "  blind rate = blind / every turn it ran in — the metric that doesn't improve "
            "with model capability.",
            "  a check at 0% across three model generations is the retirement signal (§7)."]
    return [line.rstrip() for line in out]


def _model_label(m: dict) -> str:
    """"claude-opus-5 (claude 2.1.220)" — never guessed: an absent id renders as "unknown"."""
    detail = " ".join(str(x) for x in (m.get("harness"), m.get("agent_version")) if x)
    return f"{m.get('model') or 'unknown'}{f' ({detail})' if detail else ''}"


def _scope(cwd: Path, action: str, paths: list[str], exclude_globs: list[str] | None = None) -> int:
    """`tycho scope list|set|add|remove [--exclude GLOB...]` — read or edit the scope_drift
    bounds in `.tycho.toml`. Positional globs edit the include allowlist, `--exclude` the
    denylist (exclude wins). An empty include leaves scope_drift UNSUPPORTED (zero-config).
    Globs are stored verbatim, so quote them at the shell."""
    from . import config as config_mod

    exclude = exclude_globs is not None  # --exclude carries its own globs; its presence is the mode
    globs = exclude_globs if exclude else paths
    if action != "list":
        if not globs:
            flag = " --exclude" if exclude else ""
            print(
                f"tycho scope {action}{flag}: give at least one glob, "
                f"e.g. tycho scope {action}{flag} 'src/**' 'tests/**'",
                file=sys.stderr,
            )
            return ExitCode.USAGE
        include_fns = {"set": config_mod.set_scope, "add": config_mod.add_scope, "remove": config_mod.remove_scope}
        exclude_fns = {"set": config_mod.set_exclude, "add": config_mod.add_exclude, "remove": config_mod.remove_exclude}
        (exclude_fns if exclude else include_fns)[action](cwd, globs)

    cfg = config_mod.load(cwd)
    if cfg.scope_include:
        print(f"scope ({config_mod.CONFIG_NAME}) — the agent may edit:")
        for g in cfg.scope_include:
            print(f"  {g}")
        if cfg.scope_exclude:
            print("  …except (exclude wins):")
            for g in cfg.scope_exclude:
                print(f"    !{g}")
        print("edits outside these FAIL scope_drift.")
    else:
        print("scope: none set — every path is in scope, so scope_drift stays UNSUPPORTED (zero-config).")
        print("set bounds with: tycho scope add 'src/**' 'tests/**'")
        if cfg.scope_exclude:  # exclude without include does nothing — be honest about it
            print(f"note: exclude is set but ignored while include is empty: {', '.join(cfg.scope_exclude)}")
    return ExitCode.OK


def _relay(cwd: Path, on: bool, off: bool) -> int:
    """`tycho relay [--on|--off]` — the opt-in verdict relay, off by default. On, the Stop hook
    feeds a non-VERIFIED verdict back to the agent, bounded by ``TYCHO_RELAY_MAX`` (default 3)
    so it can't loop forever. Bare ``tycho relay`` just reports the setting."""
    from . import state

    repo = state.root_for(cwd)
    if on or off:
        state.set_relay_enabled(repo, enabled=on)
    enabled = state.relay_enabled(repo)
    if not (on or off):
        print(f"tycho: verdict relay is {'ON' if enabled else 'OFF'} for {repo}"
              f"{'' if enabled else ' — verdicts stay human-only (no agent context used)'}.")
        print("  toggle: `tycho relay --on` | `--off`   ·   in Claude Code: /tycho-relay-on | "
              "/tycho-relay-off   ·   stored in .tycho.toml [relay].")
    elif enabled:
        print(f"tycho: verdict relay ON for {repo} — the agent now sees a non-VERIFIED verdict and "
              f"keeps working until VERIFIED, up to {state.relay_max()} automatic re-checks per turn. "
              f"This spends extra tokens; turn it back off with `tycho relay --off`.")
    else:
        print(f"tycho: verdict relay OFF for {repo} — verdicts stay human-only, no agent context used.")
    return ExitCode.OK


def _override(cwd: Path, check: str | None, reason: str | None,
              on: bool, off: bool, veto: bool = False, unveto: bool = False) -> int:
    """`tycho override [--on|--off|--veto|--unveto] | <check> "<reason>"` — toggle the
    capability, record a per-check override (agent), or veto one (operator). Off by default;
    overrides and vetoes are logged to .tycho/overrides.json."""
    from . import state

    repo = state.root_for(cwd)
    if veto:
        targets = [check] if check else [m["check"] for m in state.overrides(repo)]
        if not targets:
            print("tycho: no active override to veto.")
            return ExitCode.OK
        for t in targets:
            state.veto_override(repo, t)
        print(f"tycho: vetoed {', '.join(targets)} — the override no longer applies and the relay "
              f"will fire again on the next check. Lift it with `tycho override --unveto <check>`.")
        return ExitCode.OK
    if unveto:
        if not check:
            print("tycho: name the check to lift: `tycho override --unveto <check>`.")
            return ExitCode.OK
        state.unveto_override(repo, check)
        print(f"tycho: lifted the veto on {check} — it may be overridden again.")
        return ExitCode.OK
    if on or off:
        state.set_override_enabled(repo, enabled=on)
        enabled = state.override_enabled(repo)
        if enabled:
            print(f"tycho: verdict override ON for {repo} — when the relay is on, the agent may "
                  f"record `tycho override <check> \"<reason>\"`; it becomes OVERRIDDEN (agent-"
                  f"authorized, not proven) and is logged. Turn it off with `tycho override --off`.")
        else:
            print(f"tycho: verdict override OFF for {repo} — the agent cannot override verdicts.")
        return ExitCode.OK
    if check is None:  # bare status
        enabled = state.override_enabled(repo)
        print(f"tycho: verdict override is {'ON' if enabled else 'OFF'} for {repo}.")
        vetoes = state.vetoed(repo)
        if vetoes:
            print(f"  vetoed checks (not overridable): {', '.join(vetoes)} — lift with "
                  f"`tycho override --unveto <check>`.")
        print("  toggle: `tycho override --on` | `--off`   ·   in Claude Code: /tycho-override-on | "
              "/tycho-override-off   ·   stored in .tycho.toml [override].")
        return ExitCode.OK
    # record action
    from . import checks as checks_mod

    known = {c.__name__ for c in checks_mod.CHECKS}
    if not check.strip():
        print("tycho: name the check to override — `tycho override <check> \"<reason>\"`. Nothing recorded.")
        return ExitCode.OK
    if check not in known:
        print(f"tycho: unknown check {check!r}. Valid checks: {', '.join(sorted(known))}. "
              f"Nothing recorded.")
        return ExitCode.OK
    if not state.override_enabled(repo):
        print("tycho: verdict override is off here — enable it with `tycho override --on` "
              "(it stays off by default). Nothing recorded.")
        return ExitCode.OK
    if check in state.vetoed(repo):
        print(f"tycho: {check} was vetoed by the user — fix it or lift the veto "
              f"(`tycho override --unveto {check}`). Nothing recorded.")
        return ExitCode.OK
    if not reason or not reason.strip():
        print("tycho: an override needs a reason — `tycho override <check> \"<why it doesn't apply>\"`. "
              "Nothing recorded.")
        return ExitCode.OK
    state.record_override(repo, check, reason.strip())
    print(f"tycho: recorded override of {check} — \"{reason.strip()}\". It becomes OVERRIDDEN "
          f"(agent-authorized, not proven) if no adverse check survives, and is logged.")
    return ExitCode.OK


def _print_update_notice() -> None:
    """Print the 'newer version available' line, if any. Never the reason a command fails."""
    try:
        from . import version as version_mod

        note = version_mod.notice(refresh_first=True)
        if note:
            print(note, file=sys.stderr)
    except Exception:
        pass


def _is_homebrew_install() -> bool:
    """True when this is the frozen binary Homebrew installed. The realpath is the signal, since
    `<prefix>/bin/tycho` is a symlink into the Cellar. Gated on `sys.frozen` because a plain
    `pip install` into Homebrew's *Python* is also under a Cellar path but upgrades with pip."""
    if not getattr(sys, "frozen", False):
        return False
    real = os.path.realpath(sys.executable).replace("\\", "/").lower()
    return "/cellar/tycho/" in real


def _upgrade_command(force: bool = False) -> list[str]:
    """The upgrade command for however Tycho was installed, best-effort from the install path,
    falling back to pip.

    Names the **distribution** `tycho-cli`, never `tycho`: the bare `tycho` on PyPI is an
    unrelated project. `force` crosses a version pin the user set at install time; plain upgrade
    stays within it. A standalone binary can't infer its channel from `sys.prefix`, so its
    installer announces itself via ``TYCHO_INSTALL`` (npm does; Homebrew is path-detected).
    """
    from . import version as version_mod

    channel = os.environ.get("TYCHO_INSTALL", "").strip().lower()
    if channel == "npm":
        return ["npm", "install", "-g", "@swail-labs/tycho@latest"]
    if channel == "brew" or _is_homebrew_install():
        # Tap-qualified: a bare `tycho` is ambiguous if another tap ships that name.
        target = "swail-labs/tap/tycho"
        return ["brew", "reinstall", target] if force else ["brew", "upgrade", target]

    pkg = version_mod._DIST_NAME or "tycho-cli"
    prefix = sys.prefix.replace("\\", "/").lower()
    if "pipx" in prefix:
        return ["pipx", "install", "--force", pkg] if force else ["pipx", "upgrade", pkg]
    if "/uv/tools/" in prefix or "/uv/" in prefix:
        return ["uv", "tool", "install", f"{pkg}@latest"] if force else ["uv", "tool", "upgrade", pkg]
    return [sys.executable, "-m", "pip", "install", "--upgrade", pkg]


def _update(skip: bool, force: bool = False) -> int:
    """`tycho update` — check the index and upgrade in place, or `--skip` to dismiss the
    notice for this version. `--force` reinstalls the latest across a pinned version (uv/pipx);
    plain update respects the pin. Offline/failure is reported, never fatal."""
    from . import state
    from . import version as version_mod

    newest = version_mod.refresh(force=True)  # explicit command — never trust the daily cache
    behind = bool(newest) and version_mod.is_newer(newest, __version__)
    if skip:
        if behind:
            state.dismiss_update(newest)
            print(f"tycho: dismissed the update to {newest} "
                  f"(you've dismissed {state.update_dismissed_count()} so far). "
                  f"`tycho update` still upgrades when you're ready.")
        else:
            print(f"tycho {__version__}: nothing to skip — up to date (or the index is unreachable).")
        return ExitCode.OK
    if newest is None:
        print(f"tycho {__version__}: couldn't reach the package index (offline?). Try again later.")
        return ExitCode.OK
    if not behind:
        print(f"tycho {__version__} is up to date.")
        return ExitCode.OK
    cmd = _upgrade_command(force=force)
    hint = "" if force else "  (if it's pinned and doesn't move, `tycho update --force`)"
    if sys.platform == "win32":
        # A running .exe can't have its own launcher shim replaced on Windows (os error 32),
        # so defer the upgrade to a detached child that waits for us to exit. POSIX is fine.
        try:
            _spawn_deferred_upgrade(cmd)
            print(f"Updating tycho {__version__} → {newest}. The upgrade runs once this process "
                  f"exits; re-open your shell and run `tycho --version` to confirm.{hint}")
            return ExitCode.OK
        except Exception as exc:
            print(f"tycho: couldn't schedule the upgrade ({type(exc).__name__}). Run it yourself:\n  {' '.join(cmd)}",
                  file=sys.stderr)
            return ExitCode.OK
    print(f"Updating tycho {__version__} → {newest}:  {' '.join(cmd)}{hint}")
    try:
        import subprocess

        return subprocess.run(cmd).returncode or ExitCode.OK
    except Exception as exc:
        print(f"tycho: couldn't run the upgrade ({type(exc).__name__}). Run it yourself:\n  {' '.join(cmd)}",
              file=sys.stderr)
        return ExitCode.OK


def _spawn_deferred_upgrade(cmd: Sequence[str]) -> None:
    """Windows only: run `cmd` detached, after waiting on THIS PID (30s ceiling) so the running
    tycho.exe's shim is unlocked before the upgrade copies over it."""
    import os
    import subprocess

    ppid = os.getpid()
    call = "& " + " ".join("'" + str(a).replace("'", "''") + "'" for a in cmd)  # PS call-operator
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Wait-Process -Id {ppid} -Timeout 30;"
        f"{call}"
    )
    # CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP (survives our exit). NOT DETACHED_PROCESS —
    # Windows rejects it alongside CREATE_NO_WINDOW.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=flags, close_fds=True,
    )


def _install(lines: Sequence[str]) -> int:
    """Print init/uninstall status lines; exit non-zero if we refused to touch a file — a
    refusal is an unfinished install, and `tycho init --yes` in CI must fail loudly."""
    from .init import REFUSED

    for line in lines:
        print(line)
    return ExitCode.INTERNAL if any(REFUSED in line for line in lines) else ExitCode.OK


def _warn_if_hook_broken(cwd: Path) -> None:
    """Say so, loudly, if Tycho is installed here but wouldn't actually fire — a verdict looks
    identical whether the Stop hook has been running all week or dead since the venv moved.
    Stderr so it can't pollute a piped report, and never fatal."""
    try:
        from . import doctor

        for f in doctor.hook_health(cwd):
            print(f"tycho: {f.level} — {f.text}", file=sys.stderr)
            if f.fix:
                print(f"       → {f.fix}", file=sys.stderr)
    except Exception:
        pass  # a diagnostic must never be the reason a verify fails


def _offer_first_run(cwd: Path) -> None:
    """First-run 'set up Tycho here?' offer, printed for the manual commands. Never fatal."""
    try:
        from . import init as init_mod

        for line in init_mod.offer_first_run(cwd):
            print(line)
    except Exception:
        pass


def _verify(args: argparse.Namespace) -> int:
    from . import state

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
            # OpenCode's transcript is a rebuilt temp file — discovery owns cleanup.
            if not args.session and harness.name == "opencode":
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


if __name__ == "__main__":
    raise SystemExit(main())

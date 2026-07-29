"""Wiring Tycho into a machine, and into a repo — two different acts, two pairs of verbs.

    install / uninstall    this machine: the user-level harness hooks, and one line in the
                           global git excludes file
    init / off / on        this repo: the committed `.tycho.toml`, the commit trailer, and
                           whether a machine-wide install may verify here at all

`install` is the one command a new user runs, and the one `install.sh` runs for them. It
writes nothing into any repo — that is what the global excludes line buys, since
`gitignore._install_gitignore` asks `git check-ignore` rather than scanning and so
short-circuits everywhere. `init` stays self-sufficient: with no machine-wide install it
wires that repo's own hooks, so a single repo works alone.

    spelling     how a Tycho command is written, and how we recognize one
    configfile   load / merge / back up / write, defensively
    claude       settings.json, the status line, the slash commands
    harnesses    cursor, codex, opencode
    githook      the prepare-commit-msg trailer hook
    gitignore    the `.tycho/` entry in one repo
    globalignore the `.tycho/` line covering every repo

Idempotent in both directions: this module drives the sequence, and every writer below
refuses what it can't parse rather than guessing.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from ...store import config as config_mod
from ...read import harness as harness_mod
from ...read import opencode as opencode_mod
from ...store import state
from .claude import (
    _install_claude,
    _install_slash_commands as _install_slash_commands,
    _uninstall_claude,
    _uninstall_slash_commands as _uninstall_slash_commands,
)
from . import globalignore
from .configfile import REFUSED, ConfigRefused
from .githook import _install_git_hook, _uninstall_git_hook, git_hooks_dir as git_hooks_dir
from .gitignore import _install_gitignore, _uninstall_gitignore
from .installed import global_installed, installed_command
from .harnesses import (
    _install_codex,
    _install_cursor,
    _install_opencode,
    _uninstall_codex,
    _uninstall_cursor,
    _uninstall_opencode,
)
from .spelling import (
    GLOBAL,
    HARNESSES,
    REPO as REPO,
    _LOCAL_DIR,
    attest_command as attest_command,
    claude_dir,
    config_path as config_path,
    hook_argv as hook_argv,
    hook_command as hook_command,
    settings_path as settings_path,
    status_command as status_command,
)


# --- detection ---------------------------------------------------------------
# Two cheap directory probes, either sufficient: the harness has run in this repo, or is
# installed for the user. Nothing is executed.


def detect(repo: Path) -> list[str]:
    """Enabled harnesses that appear to be present, in stable order. Gated to
    `harness_mod.ENABLED_NAMES` (Claude only for now): the other installers still exist and
    are tested, but init must not prompt to wire them up."""
    return [name for name in HARNESSES if name in harness_mod.ENABLED_NAMES and _is_present(name, repo)]


def _is_present(name: str, repo: Path) -> bool:
    if (repo / _LOCAL_DIR[name]).is_dir():
        return True
    if name == "opencode":
        # OpenCode's data lives in an XDG data dir, not a ~/.<name> dotdir.
        return opencode_mod.db_path().parent.is_dir()
    return harness_mod.home(name).is_dir()


def _ask(name: str) -> bool:
    """Prompt before touching one harness. Default yes; anything but n/no is yes."""
    try:
        reply = input(f"tycho: install the {name} completion hook in this repo? [Y/n] ")
    except EOFError:
        return False
    return reply.strip().lower() not in ("n", "no")


def init(
    repo: Path,
    only: str | None = None,
    assume_yes: bool = False,
    confirm=None,
    relay_confirm=None,
) -> list[str]:
    """Install the repo-local Stop hook per harness; return status lines. Asks before each
    one. ``only`` installs just that harness and skips detection — the user named it.
    ``assume_yes`` skips the prompts for scripted/CI installs."""
    installers = {
        "claude": _install_claude,
        "cursor": _install_cursor,
        "codex": _install_codex,
        "opencode": _install_opencode,
    }
    # Adopting a repo also un-excludes it: `tycho off` then `tycho init` must mean what it
    # reads like, or the install "succeeds" and nothing ever verifies.
    state.set_excluded(repo, False)
    if only:
        names = [only]
    else:
        # Global already covers this repo — a second Stop hook would fire twice per turn. Wire
        # what global can't: the committed config and the commit trailer. That *is* adoption.
        if global_installed():
            return [
                "tycho: already verifying this repo (machine-wide install). Adopting it here:",
                *_adopt_lines(repo),
                "  Undo: `tycho off` (this repo) · `tycho uninstall` (this machine).",
            ]
        names = detect(repo)
        if not names:
            return ["no supported harness detected here — pass --harness <name> to install anyway"]
    ask = confirm or _ask
    if not assume_yes and confirm is None and not sys.stdin.isatty():
        # Nobody's there to answer, and installing unasked is the thing to avoid.
        return [f"tycho init needs a terminal to confirm ({', '.join(names)}) — pass --yes to install non-interactively"]

    lines = []
    installed = []
    for name in names:
        if not assume_yes and not ask(name):
            lines.append(f"{name}: skipped")
            continue
        try:
            lines.append(installers[name](repo))
            installed.append(name)
        except ConfigRefused as exc:
            lines.append(f"{name}{REFUSED}{exc}")
    installed_any = bool(installed)
    # Read *before* `ensure` creates one, so relay setup can tell "user configured this"
    # from "we just seeded it".
    config_existed = config_mod.path(repo).is_file()
    # Only once a harness got wired, so a declined install leaves no config behind.
    if installed_any and config_mod.ensure(repo):
        lines.append(
            f"created {config_mod.CONFIG_NAME} — no scope set yet, so nothing changes until you "
            f"run `tycho scope add '<glob>'`"
        )
    # Same rule as the config seed: no install means leave the user's repo alone.
    if installed_any:
        lines += _git_lines(repo)
        lines += _backfill(repo)
    lines += _offer_relay(
        repo, any(name in installed for name in ("claude", "codex")),
        assume_yes, config_existed, relay_confirm,
    )
    return lines


def _adopt_lines(repo: Path) -> list[str]:
    """What `tycho init` adds to a repo a machine-wide install already covers.

    `.tycho.toml` and the commit trailer, i.e. the two things that are *about this repo* and
    outlive this machine: the config a team shares and commits, and the trailer that puts a
    verdict in the git history. Deliberately not the hooks — global already runs those.
    """
    lines = []
    if config_mod.ensure(repo):
        lines.append(
            f"  created {config_mod.CONFIG_NAME} — commit it to share this repo's settings "
            f"(scope, relay) with your team"
        )
    return lines + [f"  {line}" for line in (*_git_lines(repo), *_backfill(repo))]


def _git_lines(repo: Path) -> list[str]:
    """The commit-trailer hook and the `.tycho/` gitignore entry. Neither step may raise — a
    hook we won't touch is a reported outcome, not an aborted install — and the two are
    independent, so a refused hook still leaves the gitignore."""
    lines = []
    for step in (_install_git_hook, _install_gitignore, _install_shadow):
        try:
            lines.append(step(repo))
        except ConfigRefused as exc:
            lines.append(f"git{REFUSED}{exc}")
    return [line for line in lines if line]


def _backfill(repo: Path) -> list[str]:
    """Seed the record from transcripts this repo already has.

    Runs at `init` and only when the record is empty, so it is the onboarding moment and never
    a surprise re-scan on a re-install. It writes nothing outside `.tycho/`, reads only files
    the harness wrote, and every row it adds says UNVERIFIED — but the day-one `tycho blame`
    it buys is the difference between a tool that pays off in week three and one that answers
    now. Never raises: a transcript directory we can't read is an empty backfill.
    """
    from ...store import record as record_mod
    from .. import backfill as backfill_mod

    try:
        if next(record_mod.iter_records(repo), None) is not None:
            return []  # already has history — leave it alone
        result = backfill_mod.run(repo)
    except OSError:
        return []
    return backfill_mod.summary(result) if result["turns"] else []


def _install_shadow(repo: Path) -> str:
    """Give a project with no git of its own a private one, so the checks that need a baseline
    have one from the first turn rather than the second."""
    from ...store import shadow

    if not shadow.ensure(repo):
        return ""
    # Not a `git:` line — those mean "Tycho touched your repo", and the whole point here is
    # that there is no repo to touch and none is being created in the project.
    return (
        f"tycho: no git repository here — Tycho will keep its own history in "
        f"{shadow.git_dir(repo)} (local only, never a remote, no `.git` added to your project) "
        "so edits still have a baseline to diff against"
    )


def _ask_relay() -> bool:
    """Prompt (default NO) to enable the verdict relay — it spends extra tokens, so only
    an explicit 'y'/'yes' enables it."""
    try:
        reply = input(
            "tycho: let the agent see its own verdict and keep working until VERIFIED? This feeds "
            "Tycho's verdict back into the agent (extra tokens; the loop is bounded). [y/N] "
        )
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def _offer_relay(
    repo: Path, relay_harness_installed: bool, assume_yes: bool,
    config_existed: bool, relay_confirm=None,
) -> list[str]:
    """Set up the verdict relay at install time (Claude and Codex). An existing `.tycho.toml`
    keeps its relay choice; otherwise ask (default NO). A scripted install (`--yes`, no TTY)
    never enables it silently. ``relay_confirm`` overrides the prompt for tests."""
    if not relay_harness_installed:
        return []
    if config_existed:
        state_txt = "ON" if state.relay_enabled(repo) else "OFF"
        return [
            f"tycho: verdict relay is {state_txt} (from your {config_mod.CONFIG_NAME}). Change it "
            f"with `tycho relay --on|--off`."
        ]
    interactive = relay_confirm is not None or (not assume_yes and sys.stdin.isatty())
    if not interactive:
        return []  # scripted / no TTY: leave the default (off), already written by config.ensure
    if (relay_confirm or _ask_relay)():
        state.set_relay_enabled(repo, True)
        return [
            f"tycho: verdict relay ON — the agent will see a non-VERIFIED verdict and keep working "
            f"until VERIFIED, up to {state.relay_max()} automatic re-checks per turn (extra tokens). "
            f"Turn it off with `tycho relay --off` or /tycho-relay-off."
        ]
    return [
        "tycho: verdict relay OFF (the default) — turn it on with `tycho relay --on` "
        "to have the agent work until VERIFIED."
    ]


def wired_here(repo: Path) -> bool:
    """Does this repo have an install of its own — as opposed to being covered by the
    machine-wide one? What `tycho uninstall` checks before assuming what you meant."""
    return bool(state.read_install(repo)) or any(_wired_here(repo, n) for n in HARNESSES)


def _wired_here(repo: Path, name: str) -> bool:
    """Does `name`'s config already carry our hook? A config we can't read counts as wired —
    we don't offer to overwrite something we can't inspect."""
    try:
        return installed_command(repo, name) is not None
    except ConfigRefused:
        return True


def _ask_setup(harnesses: list[str]) -> bool:
    reply = input(f"Set up Tycho in this repo ({', '.join(harnesses)})? [Y/n] ").strip().lower()
    return reply in ("", "y", "yes")


def offer_first_run(repo: Path, confirm=None) -> list[str]:
    """First-run nudge, for someone who has a supported agent and no Tycho *anywhere*.

    Narrow on purpose. Once a machine-wide install exists this has nothing to say — the repo
    is already being verified, and `hook._first_seen` is what announces that. Offering to
    "set up" a repo Tycho is already watching is how a diagnostic starts contradicting itself
    (`doctor` printed exactly that on a repo the user had just switched off).
    """
    if state.already_offered(repo) or state.excluded(repo) or global_installed():
        return []
    detected = detect(repo)
    if not detected or state.read_install(repo) or any(_wired_here(repo, n) for n in detected):
        return []  # nothing here, or already set up
    state.mark_offered(repo)  # offered — regardless of the answer, don't ask again
    ask = confirm or _ask_setup
    interactive = confirm is not None or sys.stdin.isatty()  # a real TTY, or a test's confirm
    if interactive and ask(detected):
        return ["Tycho: setting up here…", *init(repo, only=None, assume_yes=True)]
    return [
        f"Tycho isn't set up yet — a supported agent ({', '.join(detected)}) is here.",
        "  `tycho install` covers every repo on this machine; `tycho init` just this one.",
        "  (offered once; it won't ask again)",
    ]


# --- `tycho install`: the machine-wide setup (strategy §6.7) -----------------
#
# The one command a new user runs, and the one the installer runs for them. It writes the
# user-level Claude Code config plus one line in the user's global git excludes file — and
# that second write is what lets it leave every repo alone: with `.tycho/` globally ignored,
# `_install_gitignore` short-circuits on `git check-ignore` everywhere, so a machine-wide
# install never edits a tracked file in a repo nobody opted in.
#
# Blast radius is still the cost, so: named in full before anything is written, guarded at run
# time (`_GLOBAL_GUARD` — no-op outside a git repo, defers to any per-repo install), additive
# only, and no `core.hooksPath`, which set machine-wide silently disables every repo's own
# `.git/hooks` (husky, pre-commit, lefthook).


def _present_for_user() -> list[str]:
    """Enabled harnesses installed for this user — the machine-wide equivalent of `detect`,
    which also accepts a repo-local directory. Asked by name rather than assuming one, so
    widening `ENABLED_NAMES` widens this with it."""
    return [n for n in HARNESSES
            if n in harness_mod.ENABLED_NAMES and harness_mod.home(n).is_dir()]


def _global_targets() -> list[Path]:
    home = claude_dir(Path.cwd(), GLOBAL)
    return [home / "settings.json", home / "commands", globalignore.path()]


def _ask_install() -> bool:
    """Consent for the machine-wide setup: name every path, then default to YES.

    `[Y/n]`, unlike the `[y/N]` this replaced. That prompt guarded an escalation the user had
    not asked for — `tycho init --global`, typed inside one repo. This one *is* what they came
    to do, so a bare Enter should do it. The path list stays: the blast radius did not shrink
    because the default flipped.
    """
    print("tycho: set up Tycho for every repo on this machine. This writes:")
    for path in _global_targets():
        print(f"    {path}")
    print("  Tycho then verifies every git repo you open and writes nothing into them.")
    print("  It stays quiet outside git repos and defers to any per-repo install.")
    print("  Undo any time: `tycho uninstall` (machine) · `tycho off` (one repo).")
    try:
        reply = input("tycho: use recommended settings? [Y/n] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("", "y", "yes")


def install(assume_yes: bool = False, confirm=None, ignore_confirm=None) -> list[str]:
    """`tycho install` — set Tycho up for this machine. The one-command path.

    ``ignore_confirm`` decides the global-gitignore line separately, for the `--no-defaults`
    walk-through; by default it rides on the single yes, because it is what makes the install
    leave repos alone and answering it separately is a question with one sensible answer.
    """
    repo = Path.cwd()
    if not _present_for_user():
        return ["tycho: no supported agent harness found for this user — nothing to set up."]
    ask = confirm or _ask_install
    if not assume_yes:
        if confirm is None and not sys.stdin.isatty():
            return ["tycho install needs a terminal to confirm — pass --yes to install "
                    "non-interactively"]
        if not ask():
            return ["tycho: setup skipped — nothing written."]
    try:
        lines = [_install_claude(repo, GLOBAL)]
    except ConfigRefused as exc:
        return [f"claude (global){REFUSED}{exc}"]
    want_ignore = ignore_confirm() if ignore_confirm is not None else True
    if want_ignore:
        try:
            if (line := globalignore.install()) is not None:
                lines.append(line)
        except ConfigRefused as exc:
            # A read-only config dir is not a failed install: the per-repo `.gitignore` step
            # still covers `.tycho/`, one repo at a time.
            lines.append(f"git{REFUSED}{exc}")
    lines.append("tycho: live in every git repo on this machine — nothing to run per repo.")
    if not want_ignore:
        lines.append("  `.tycho/` will be added to each repo's .gitignore as it's first used.")
    lines.append("  `tycho init` in a repo adds the commit trailer and a shareable config.")
    lines.append("  Undo: `tycho uninstall` (machine) · `tycho off` (one repo).")
    return lines


def init_global(assume_yes: bool = False, confirm=None) -> list[str]:
    """`tycho init --global` — kept as the old spelling of `tycho install`, so scripts and
    muscle memory from 0.1.x/0.2.0 keep working."""
    return install(assume_yes=assume_yes, confirm=confirm)


def uninstall_global() -> list[str]:
    """`tycho uninstall` — the exact inverse of `install`, including the global gitignore
    line. Idempotent. Repo-local installs are deliberately untouched: separate decisions, and
    `tycho off` is the one that speaks about a repo."""
    lines = []
    try:
        lines.append(_uninstall_claude(Path.cwd(), GLOBAL))
    except ConfigRefused as exc:
        lines.append(f"claude (global){REFUSED}{exc}")
    try:
        if (line := globalignore.uninstall()) is not None:
            lines.append(line)
    except ConfigRefused as exc:
        lines.append(f"git{REFUSED}{exc}")
    return lines


# --- `tycho off` / `tycho on`: one repo, under a machine-wide install --------


def off(repo: Path, purge: bool = False) -> list[str]:
    """`tycho off` — stop verifying this repo, whichever way it is currently wired.

    One meaning, two mechanisms: record the machine-level exclusion, and remove any repo-local
    hooks. Before this, a user under a machine-wide install who typed `tycho uninstall` got
    four "nothing to remove" lines and kept being verified — the command reported success at
    doing nothing.

    `.tycho.toml` and `.tycho/` are deliberately left alone. The config may be committed and
    shared, and the record is history; neither does anything while nothing is running, and
    deleting them is what `--purge` is for.
    """
    lines = []
    if wired_here(repo):
        lines += uninstall(repo)
    changed = state.set_excluded(repo, True)
    lines.append(
        "tycho: off for this repo." if changed
        else "tycho: already off for this repo."
    )
    if purge:
        lines += _purge_repo_local(repo)
    lines.append("  Turn it back on here: `tycho on`. Adopt it properly: `tycho init`.")
    return lines


def on(repo: Path) -> list[str]:
    """`tycho on` — resume verifying this repo. Says so plainly when nothing would run
    anyway, because "on" over an uninstalled Tycho is the kind of quiet lie this tool exists
    to catch."""
    changed = state.set_excluded(repo, False)
    lines = ["tycho: on for this repo." if changed else "tycho: already on for this repo."]
    if not global_installed() and not state.read_install(repo):
        lines.append(
            "  Nothing is wired to run here yet — `tycho install` (this machine) or "
            "`tycho init` (this repo)."
        )
    return lines


def uninstall(repo: Path, only: str | None = None, purge: bool = False) -> list[str]:
    """Remove Tycho-owned hook entries; return status lines. Only ours come out — unrelated
    hooks, keys and user-owned groups stay. ``purge`` also deletes the repo-local `.tycho/`
    and `.tycho.toml`; never the default, since it drops the catch trail."""
    removers = {
        "claude": _uninstall_claude,
        "cursor": _uninstall_cursor,
        "codex": _uninstall_codex,
        "opencode": _uninstall_opencode,
    }
    lines = []
    for name, fn in removers.items():
        if only and name != only:
            continue
        try:
            lines.append(fn(repo))
            # Only after the config write succeeded, or `doctor` diagnoses a ghost.
            state.drop_install(repo, name)
        except ConfigRefused as exc:
            # Same rule as install: a config we can't parse is one we can't safely rewrite.
            lines.append(f"{name}{REFUSED}{exc}")
    # Shared by every harness, so they only come out once nothing is wired here any more —
    # else `uninstall --harness x` strips the trailer the harness you kept relies on.
    if not state.read_install(repo):
        try:
            lines += filter(None, [_uninstall_git_hook(repo), _uninstall_gitignore(repo)])
        except ConfigRefused as exc:
            lines.append(f"git{REFUSED}{exc}")
    if purge:
        lines += _purge_repo_local(repo)
    return lines


def _clear_readonly(func, path, _exc) -> None:
    """Retry a failed unlink after clearing the read-only bit.

    Git writes loose objects and packs read-only, and on Windows `unlink` refuses a read-only
    file outright — so `--purge` left the whole shadow repo behind in a project that has no git
    of its own. POSIX doesn't need this (the directory's write bit governs), and a chmod that
    still doesn't help re-raises into the caller's handler."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


# 3.12 renamed `onerror` to `onexc` and warns on the old name; 3.11 only knows the old one.
_RMTREE_ONFAIL = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _purge_repo_local(repo: Path) -> list[str]:
    """Delete the two repo-local artifacts Tycho owns whole: `.tycho/` and `.tycho.toml`.
    Idempotent. The machine-wide state under `~/.local/share/tycho` (the all-time tally,
    shared across repos) is never touched."""
    lines = []
    for path in (state.dir_for(repo), config_mod.path(repo)):
        try:
            if path.is_dir():
                shutil.rmtree(path, **{_RMTREE_ONFAIL: _clear_readonly})
                lines.append(f"removed {path.name}/")
            elif path.exists():
                path.unlink()
                lines.append(f"removed {path.name}")
            else:
                lines.append(f"{path.name}: nothing to remove")
        except OSError as exc:
            # An unfinished uninstall must exit non-zero, like a config we couldn't rewrite.
            lines.append(f"{path.name}{REFUSED}{exc.strerror or exc}")
    return lines


# --- the facade -------------------------------------------------------------
#
# Names other modules and the tests reach for through `install.`. Listed explicitly rather
# than star-imported so this file states the package's whole surface in one place.

from .claude import (  # noqa: E402
    _SLASH_MARKER as _SLASH_MARKER,
    _STATUS_REFRESH_MS as _STATUS_REFRESH_MS,
    _commands_dir as _commands_dir,
    _slash_files as _slash_files,
)
from .githook import (  # noqa: E402
    _GIT_BEGIN as _GIT_BEGIN,
    _GIT_END as _GIT_END,
)
from .gitignore import _IGNORE_ENTRY as _IGNORE_ENTRY  # noqa: E402
from .spelling import (  # noqa: E402
    _GLOBAL_GUARD as _GLOBAL_GUARD,
    _is_tycho_hook as _is_tycho_hook,
    _is_tycho_owned as _is_tycho_owned,
    _is_tycho_prompt_submit as _is_tycho_prompt_submit,
    _is_tycho_session_start as _is_tycho_session_start,
    _is_tycho_status as _is_tycho_status,
    _quote_program as _quote_program,
)

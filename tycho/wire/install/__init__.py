"""`tycho init` / `tycho uninstall` — manage the completion hook in each harness's config.

Writes are repo-local by default; a harness's `$HOME` dir is read, never written, as a
detection signal. The one exception is `--global` (`init_global`), which writes the
user-level Claude config behind an explicit prompt.

    spelling     how a Tycho command is written, and how we recognize one
    configfile   load / merge / back up / write, defensively
    claude       settings.json, the status line, the slash commands
    harnesses    cursor, codex, opencode
    githook      the prepare-commit-msg trailer hook
    gitignore    the `.tycho/` entry

Idempotent both ways: this module drives the sequence, and every writer below refuses what
it can't parse rather than guessing.
"""

from __future__ import annotations

import shutil
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
    settings_path,
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
    if only:
        names = [only]
    else:
        # Global already covers this repo — a second Stop hook per turn. Wire only the
        # commit trailer, which global can't.
        if global_installed():
            return [
                "tycho: a global install is active — the Claude Code hooks already cover this "
                f"repo ({settings_path(repo, GLOBAL)}).",
                *_git_lines(repo),
                "  Per-repo anyway: `tycho init --harness claude` (global then defers to it).",
                "  Remove the global install: `tycho uninstall --global`.",
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
    lines += _offer_relay(
        repo, any(name in installed for name in ("claude", "codex")),
        assume_yes, config_existed, relay_confirm,
    )
    return lines


def _git_lines(repo: Path) -> list[str]:
    """The commit-trailer hook and the `.tycho/` gitignore entry. Neither step may raise — a
    hook we won't touch is a reported outcome, not an aborted install — and the two are
    independent, so a refused hook still leaves the gitignore."""
    lines = []
    for step in (_install_git_hook, _install_gitignore):
        try:
            lines.append(step(repo))
        except ConfigRefused as exc:
            lines.append(f"git{REFUSED}{exc}")
    return [line for line in lines if line]


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
    """First-run nudge: a supported agent is here but Tycho isn't wired. Fires once per
    repo and never writes config without consent — the "already offered" marker lives
    outside the repo, and a decline touches nothing."""
    if state.already_offered(repo):
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
        f"Tycho isn't set up in this repo yet — a supported agent ({', '.join(detected)}) is here.",
        "  Run `tycho init` to wire it up (offered once; it won't ask again).",
    ]


# --- the machine-wide install (strategy §6.7) --------------------------------
#
# `tycho init --global` writes the user-level Claude Code config once. Nothing below writes a
# `.tycho.toml` — zero-config stays zero-config. Blast radius is the whole cost here, so:
# opt-in and loud (`_ask_global` names every path, defaults to NO), guarded at run time
# (`_GLOBAL_GUARD`), additive only, and no `core.hooksPath` — set machine-wide that silently
# disables every repo's own `.git/hooks` (husky, pre-commit, lefthook).


def _global_targets() -> list[Path]:
    home = claude_dir(Path.cwd(), GLOBAL)
    return [home / "settings.json", home / "commands"]


def _ask_global() -> bool:
    """Consent for a machine-wide install: name every path, then default to NO. `[y/N]`, not
    the per-repo `[Y/n]` — this fires in repos the user never opted in, so a bare Enter must
    not install it."""
    print("tycho: this installs Tycho for EVERY repo on this machine. It will write:")
    for path in _global_targets():
        print(f"    {path}")
    print("  It only runs inside a git repo, defers to any per-repo install, and never")
    print("  touches your global git config. Undo at any time with `tycho uninstall --global`.")
    try:
        reply = input("tycho: install globally? [y/N] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def init_global(assume_yes: bool = False, confirm=None) -> list[str]:
    """`tycho init --global` — wire Tycho into the user-level harness config. Installs
    nothing without an explicit yes; `assume_yes` is the scripted path, and neither that
    nor a TTY means we print how to proceed instead of guessing."""
    repo = Path.cwd()
    if not harness_mod.home("claude").is_dir():
        return ["tycho: Claude Code isn't installed for this user — nothing to wire globally."]
    ask = confirm or _ask_global
    if not assume_yes:
        if confirm is None and not sys.stdin.isatty():
            return ["tycho init --global needs a terminal to confirm — pass --yes to install "
                    "non-interactively"]
        if not ask():
            return ["tycho: global install skipped — nothing written."]
    try:
        lines = [_install_claude(repo, GLOBAL)]
    except ConfigRefused as exc:
        return [f"claude (global){REFUSED}{exc}"]
    lines.append("tycho: live in every git repo on this machine. It stays quiet outside git "
                 "repos and defers to any per-repo install.")
    lines.append("  The commit trailer is per-repo — run `tycho init` in a repo to add it.")
    lines.append("  Undo: `tycho uninstall --global`.")
    return lines


def uninstall_global() -> list[str]:
    """`tycho uninstall --global` — the exact inverse of `init_global`. Idempotent.
    Repo-local installs are deliberately not touched: separate, explicit decisions."""
    try:
        return [_uninstall_claude(Path.cwd(), GLOBAL)]
    except ConfigRefused as exc:
        return [f"claude (global){REFUSED}{exc}"]


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


def _purge_repo_local(repo: Path) -> list[str]:
    """Delete the two repo-local artifacts Tycho owns whole: `.tycho/` and `.tycho.toml`.
    Idempotent. The machine-wide state under `~/.local/share/tycho` (the all-time tally,
    shared across repos) is never touched."""
    lines = []
    for path in (state.dir_for(repo), config_mod.path(repo)):
        try:
            if path.is_dir():
                shutil.rmtree(path)
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

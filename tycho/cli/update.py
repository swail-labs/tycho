"""`tycho update` — is a newer Tycho out, and how would *this* install get it?

The upgrade command is inferred from the install path (uv / pipx / pip / npm / Homebrew), so
what we print is what the user can actually run.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Sequence

from .. import __version__
from . import ExitCode

def _print_update_notice() -> None:
    """Print the 'newer version available' line, if any. Never the reason a command fails."""
    try:
        from ..wire import version as version_mod

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


# The README's one-liner. `https://swail.dev/install.sh` (what install.sh's own header used to
# claim) answers 405 — the path is /tycho/.
_INSTALLER_URL = "https://swail.dev/tycho/install.sh"


def _reinstall_command() -> list[str]:
    """Re-run the installer against the directory this binary already occupies.

    `install.sh` verifies the download against the release's SHA256SUMS and then overwrites
    `<dir>/tycho` in place, so a re-run *is* the upgrade — there's nothing else to call.

    The directory is passed explicitly rather than left to the installer's `~/.local/bin` default:
    `TYCHO_INSTALL_DIR` lets the binary live anywhere, and defaulting would write a second copy to
    `~/.local/bin` while the one actually on PATH stayed stale — an upgrade that reports success
    and changes nothing.
    """
    dest = os.path.dirname(os.path.realpath(sys.executable))
    return ["sh", "-c", f"curl -fsSL {_INSTALLER_URL} | TYCHO_INSTALL_DIR={shlex.quote(dest)} sh"]


def _upgrade_command(force: bool = False) -> list[str]:
    """The upgrade command for however Tycho was installed, best-effort from the install path,
    falling back to pip.

    Names the **distribution** `tycho-cli`, never `tycho`: the bare `tycho` on PyPI is an
    unrelated project. `force` crosses a version pin the user set at install time; plain upgrade
    stays within it. A standalone binary can't infer its channel from `sys.prefix`, so its
    installer announces itself via ``TYCHO_INSTALL`` (npm does; Homebrew is path-detected).

    Anything else frozen came from `install.sh` (the README's curl one-liner) or a hand-downloaded
    release asset. Neither leaves a marker, but neither needs one: a frozen binary that is not npm
    and not Homebrew has no other way to upgrade itself, so re-running the installer IS the
    channel. Without this branch it fell through to the pip default and ran
    ``<the binary> -m pip install --upgrade tycho-cli`` — the binary invoked with pip's arguments,
    which no standalone build can serve. ``TYCHO_INSTALL=direct`` forces it.
    """
    from ..wire import version as version_mod

    channel = os.environ.get("TYCHO_INSTALL", "").strip().lower()
    if channel == "npm":
        return ["npm", "install", "-g", "@swail-labs/tycho@latest"]
    if channel == "brew" or _is_homebrew_install():
        # Tap-qualified: a bare `tycho` is ambiguous if another tap ships that name.
        target = "swail-labs/tap/tycho"
        return ["brew", "reinstall", target] if force else ["brew", "upgrade", target]
    # `install.sh` is POSIX-only, so a frozen Windows binary has no installer to re-run and keeps
    # the old fallback; the npm wrapper is the supported standalone channel there.
    if channel == "direct" or (getattr(sys, "frozen", False) and sys.platform != "win32"):
        return _reinstall_command()

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
    from tycho import cli  # by attribute: tests monkeypatch `cli._upgrade_command`
    from ..store import state
    from ..wire import version as version_mod

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
    cmd = cli._upgrade_command(force=force)
    hint = "" if force else "  (if it's pinned and doesn't move, `tycho update --force`)"
    if sys.platform == "win32":
        # A running .exe can't have its own launcher shim replaced on Windows (os error 32),
        # so defer the upgrade to a detached child that waits for us to exit. POSIX is fine.
        try:
            cli._spawn_deferred_upgrade(cmd)
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

        rc = subprocess.run(cmd).returncode or ExitCode.OK
    except Exception as exc:
        print(f"tycho: couldn't run the upgrade ({type(exc).__name__}). Run it yourself:\n  {' '.join(cmd)}",
              file=sys.stderr)
        return ExitCode.OK
    for line in _setup_nudge():
        print(line)
    return rc


def _setup_nudge() -> list[str]:
    """Said after a successful upgrade, to someone who wired Tycho up per repo.

    Upgrading the package deliberately does not re-run any install — so without this, a 0.1.x
    user upgrades into the machine-wide model and never hears that it exists. `doctor` would
    tell them, but the person who has to be told is exactly the person not running `doctor`.
    Silent once they're set up.
    """
    from ..wire import install as init_mod

    try:
        if init_mod.global_installed():
            return []
    except Exception:
        return []
    return ["", "tycho: `tycho install` sets Tycho up once for every repo on this machine — "
                "no more running `tycho init` per repo."]


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

"""The `.tycho/` line in the user's *global* git excludes file.

One write, at `tycho install` time, to a file that belongs to the user — and then Tycho never
touches any repo's `.gitignore`, in any repo, ever. `gitignore._install_gitignore` already
asks `git check-ignore` rather than scanning, so a global entry short-circuits it everywhere.

That is what makes a machine-wide install leave no trace: without it, every repo the agent
opens grows an untracked `.tycho/` in `git status`, and the only fix is editing a *tracked*
file in a repo the user never asked us to touch.

Where the line goes, in git's own order of precedence:

  1. `core.excludesFile`, if the user set one — theirs, so we append to it
  2. otherwise `$XDG_CONFIG_HOME/git/ignore` (default `~/.config/git/ignore`), which git reads
     with no configuration at all — so this needs no `git config` write, and cannot change
     how any other tool behaves

Same manners as the per-repo entry: append, never rewrite; idempotent; removed byte-for-byte
on uninstall.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .configfile import _current_text, _write_text
from .gitignore import _IGNORE_ENTRY, _IGNORE_NOTE


_GLOBAL_NOTE = f"{_IGNORE_NOTE} Added by `tycho install`; removed by `tycho uninstall`."


_BLOCK = f"{_GLOBAL_NOTE}\n{_IGNORE_ENTRY}\n"


def configured_path() -> Path | None:
    """The user's own `core.excludesFile`, or None when they never set one."""
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", "core.excludesFile"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return None
    value = (proc.stdout or "").strip()
    return Path(value).expanduser() if proc.returncode == 0 and value else None


def default_path() -> Path:
    """Where git looks with no configuration: `$XDG_CONFIG_HOME/git/ignore`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "git" / "ignore"


def path() -> Path:
    """The file this module writes: the user's, if they have one; else git's default."""
    return configured_path() or default_path()


def installed() -> bool:
    """Is our line already there? Read of the exact file we would write."""
    text = _current_text(path())
    return text is not None and _IGNORE_ENTRY in text.splitlines()


def install() -> str | None:
    """Add `.tycho/` to the global excludes file. None when there was nothing to do.

    Never raises past `ConfigRefused`: a machine-wide install must not die because a config
    directory is read-only, and the per-repo `.gitignore` fallback still covers that case.
    """
    target = path()
    if installed():
        return None
    current = _current_text(target)
    text = _BLOCK if current is None else f"{current}\n{_BLOCK}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if not _write_text(target, text, backup=current is not None):
        return None
    return f"git: ignored {_IGNORE_ENTRY} for every repo → {target}"


def uninstall() -> str | None:
    """Take our two lines back out, leaving everything else byte-for-byte. None when absent."""
    target = path()
    current = _current_text(target)
    if not current or _BLOCK not in current:
        return None
    # Longest match first, so the separator we added leaves with the block it belongs to.
    for pattern, replacement in ((f"\n\n{_BLOCK}", "\n"), (f"\n{_BLOCK}", ""), (_BLOCK, "")):
        if pattern in current:
            text = current.replace(pattern, replacement, 1)
            break
    else:
        return None
    if not text.strip():
        try:  # nothing survives but whitespace — the file was ours
            target.unlink()
        except OSError:
            return None
        return f"git: removed the global excludes file Tycho created → {target}"
    if not _write_text(target, text, backup=True):
        return None
    return f"git: stopped ignoring {_IGNORE_ENTRY} globally → {target}"

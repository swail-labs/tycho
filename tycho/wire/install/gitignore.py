"""The `.tycho/` entry in the repo's `.gitignore`.

Machine-local state, and `turns.jsonl` carries the agent's own prose. Appended politely and
restored byte-for-byte on uninstall.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...read import gitstate
from .configfile import _current_text, _drop_backup, _write_text


# Machine-local, and `turns.jsonl` carries the agent's own prose. Not wanted in a PR.
_IGNORE_ENTRY = ".tycho/"


_IGNORE_NOTE = "# Tycho's machine-local state (tally, heartbeat, turn record)."


_IGNORE_BLOCK = f"{_IGNORE_NOTE}\n{_IGNORE_ENTRY}\n"


def _already_ignored(repo: Path) -> bool:
    """Is `.tycho/` already ignored here, by any means? Asks git rather than scanning
    `.gitignore`: the entry can live in a parent `.gitignore`, `.git/info/exclude`, or the
    global excludesfile. Any error reads as "not ignored"."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", _IGNORE_ENTRY],
            capture_output=True,
        )
    except (OSError, ValueError):
        return False
    return proc.returncode == 0


def _install_gitignore(repo: Path) -> str | None:
    """Add `.tycho/` to the repo's `.gitignore`. None when there is nothing to do. Appended
    rather than rewritten, backed up first, and never the reason an install fails."""
    if not gitstate.is_repo(repo) or _already_ignored(repo):
        return None
    path = repo / ".gitignore"
    current = _current_text(path)
    if current is not None and _IGNORE_ENTRY in current.splitlines():
        return None  # present but not effective (negated later, say) — not ours to fight
    if current is None:
        text = _IGNORE_BLOCK
    else:
        # One newline, then ours: a file that already ended in one gets a blank separator
        # and one that didn't gets none, which is how uninstall tells them apart.
        text = f"{current}\n{_IGNORE_BLOCK}"
    if not _write_text(path, text, backup=current is not None):
        return None
    return f"git: ignored {_IGNORE_ENTRY} → {path}"


def _uninstall_gitignore(repo: Path) -> str | None:
    """Take our two lines back out, leaving every other line exactly as it was."""
    path = repo / ".gitignore"
    current = _current_text(path)
    if not current or _IGNORE_BLOCK not in current:
        return None
    # Longest match first, so the separator we added comes out with the block —
    # `init; uninstall` must restore the file byte-for-byte.
    for pattern, replacement in ((f"\n\n{_IGNORE_BLOCK}", "\n"), (f"\n{_IGNORE_BLOCK}", ""),
                                 (_IGNORE_BLOCK, "")):
        if pattern in current:
            text = current.replace(pattern, replacement, 1)
            break
    else:
        return None
    if not text.strip():
        # Nothing survives but whitespace, so the file was ours.
        try:
            path.unlink()
        except OSError:
            return None
        _drop_backup(path)
        return f"git: removed the {path.name} Tycho created → {path}"
    if not _write_text(path, text, backup=True):
        return None
    _drop_backup(path)
    return f"git: stopped ignoring {_IGNORE_ENTRY} → {path}"

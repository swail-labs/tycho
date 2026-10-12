"""Tycho's block in the repo's `.gitignore`: `.tycho/`, and the `*.tycho.bak` files init
leaves beside a config it rewrote.

Machine-local state, and `turns.jsonl` carries the agent's own prose. Everything in the block
is Tycho's own — nothing here ignores a file the user wrote. Appended politely and restored
byte-for-byte on uninstall.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...read import gitstate
from .configfile import _current_text, _drop_backup, _write_text


# Machine-local, and `turns.jsonl` carries the agent's own prose. Not wanted in a PR.
_IGNORE_ENTRY = ".tycho/"


_IGNORE_NOTE = "# Tycho's machine-local state (tally, heartbeat, turn record)."


# The `.bak` init leaves beside a config it rewrites is ours too — one ignore block covers
# both, rather than a second mechanism for our own litter.
_IGNORE_BAK = "*.tycho.bak"


# The block as installs before 0.2.2 wrote it: still out there, so install upgrades it in
# place and uninstall must still recognize it.
_LEGACY_BLOCK = f"{_IGNORE_NOTE}\n{_IGNORE_ENTRY}\n"


_IGNORE_BLOCK = f"{_LEGACY_BLOCK}{_IGNORE_BAK}\n"


def _blocks(block: str, legacy: str) -> list[tuple[str, str]]:
    """Removal patterns, longest match and newest block first — the older block is a prefix
    of the current one, so matching it first would strand the `.bak` line."""
    return [p for b in (block, legacy)
            for p in ((f"\n\n{b}", "\n"), (f"\n{b}", ""), (b, ""))]


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
    if not gitstate.is_repo(repo):
        return None
    path = repo / ".gitignore"
    current = _current_text(path)
    if current is not None and _LEGACY_BLOCK in current and _IGNORE_BLOCK not in current:
        # Our own older block — it ignores `.tycho/`, so the check below would call this done
        # and leave every `.bak` we write untracked forever. Swap it, don't stack a second.
        text = current.replace(_LEGACY_BLOCK, _IGNORE_BLOCK, 1)
        return f"git: ignored {_IGNORE_BAK} → {path}" if _write_text(path, text) else None
    if _already_ignored(repo):
        return None
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
    """Take our block back out, leaving every other line exactly as it was."""
    path = repo / ".gitignore"
    current = _current_text(path)
    if not current or _LEGACY_BLOCK not in current:  # a prefix of both blocks
        return None
    # Longest match first, so the separator we added comes out with the block —
    # `init; uninstall` must restore the file byte-for-byte.
    for pattern, replacement in _blocks(_IGNORE_BLOCK, _LEGACY_BLOCK):
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

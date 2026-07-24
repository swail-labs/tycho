"""Read-only git reader via subprocess. Never writes to the repo."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run a git command in `repo`; return (returncode, stdout). Never raises on
    non-zero — callers decide what a failure means."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def is_repo(repo: Path) -> bool:
    code, _ = _git(repo, "rev-parse", "--git-dir")
    return code == 0


def head_sha(repo: Path) -> str | None:
    code, out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else None


def commit_exists(repo: Path, ref: str) -> bool:
    code, _ = _git(repo, "cat-file", "-e", f"{ref}^{{commit}}")
    return code == 0


def diff_names(repo: Path, since: str) -> tuple[str, ...]:
    """Paths changed between `since` and the working tree (staged + unstaged)."""
    code, out = _git(repo, "diff", "--name-only", since)
    if code != 0:
        return ()
    return tuple(line for line in out.splitlines() if line)


def blob_at(repo: Path, ref: str, path: str) -> str | None:
    """File contents at a ref (e.g. `HEAD`), or None if absent there."""
    code, out = _git(repo, "show", f"{ref}:{path}")
    return out if code == 0 else None

"""A private git repository Tycho keeps for projects that have none of their own.

Half of what Tycho knows about a turn comes from git: the pre-edit baseline the tamper checks
diff against, whether an edit reconciled with the tree, what `review` and `blame` read. In a
project without git, all of that degrades — the baseline falls back to whatever the harness
happened to attach to an edit, and `originalFile: null` on a repeat edit means no baseline at
all.

So Tycho keeps its own. One commit per verified turn, into `.tycho/shadow.git`, with the
project as the work tree. It is never a remote, never pushed, never a second `.git` in the
project directory, and it is used **only** when the project is not already in a real repo —
a project with git keeps git, and this file does nothing.

Ordering matters and is the whole trick: a turn is verified *before* its commit is made, so
`HEAD` during verification is the end of the previous turn — exactly the "before" state the
checks need. The commit is the last thing that happens, which also means an interrupted hook
loses a commit rather than corrupting a baseline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import state

_DIR = "shadow.git"

# Directories that are not project source: dependencies, build output, caches. Shared with the
# repo scan (`read/session.py`), which skips exactly these when looking for tests — one
# vocabulary, so the two cannot drift the way the test-path rules once did.
IGNORED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "vendor", "target", "dist", "build",
    "bazel-out", "out", "bin", "obj", "Pods", "coverage",
})

# What the shadow never records. Tycho's own bookkeeping first — the ledger changes every turn
# and would make every commit look like a change to the project. Then the noise `.gitignore`
# would normally handle, which a project without git does not have: unignored, a gitless JS
# project commits `node_modules` on every single turn.
_EXCLUDE = "\n".join([
    ".tycho/", ".tycho.toml", ".claude/commands/",
    *(f"{d}/" for d in sorted(IGNORED_DIRS)),
    "__pycache__/", "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dylib",
    ".DS_Store", "*.log", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/",
]) + "\n"


_GIT_MARKER = ".git"


def real_repo_root(repo: Path) -> Path | None:
    """The project's own git root, or None when it has none.

    A filesystem walk, not `rev-parse`: this decides how every git call is *routed*, so it
    cannot itself be one. `.git` is a directory in a normal clone and a file in a worktree or
    submodule — both count. It lives here rather than in the git reader because `store` sits
    below `read`, and the reader needs to ask this question before it can run anything.
    """
    try:
        candidates = (repo, *repo.parents)
    except (OSError, ValueError):
        return None
    for d in candidates:
        if (d / _GIT_MARKER).exists():
            return d
    return None


def git_dir(repo: Path) -> Path:
    return state.dir_for(repo) / _DIR


def _run(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "--git-dir", str(git_dir(repo)), "--work-tree", str(repo),
             "-c", "core.quotePath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def _plain(*args: str, timeout: int = 30) -> int:
    """A git call with no `--git-dir`/`--work-tree` of its own — just `init`."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=timeout).returncode
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1


def exists(repo: Path) -> bool:
    return git_dir(repo).is_dir()


def ensure(repo: Path) -> bool:
    """Create the shadow repo if this project needs one. True if one is now available.

    Needed means: the project is not in a real git repo. Called from `tycho init` and from the
    hook, so a project that stops being a git repo — or was never one — still gets a baseline.
    """
    if real_repo_root(repo) is not None:
        return False
    if exists(repo):
        return True
    d = git_dir(repo)
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    # Two steps rather than one, on purpose. `git --git-dir=X --work-tree=Y init` does work in
    # a single call, but it bakes an absolute `core.worktree` into the config, so moving or
    # copying the project silently points its shadow at the old path. Initializing bare and
    # then clearing `core.bare` leaves no path in the config at all: the work tree comes from
    # the caller on every command, so the project stays relocatable.
    #
    # `init` is also the one command that must NOT carry `--work-tree` — `--bare` means "no
    # work tree", so git rejects the pair (identically on 2.39 and 2.55; not a version quirk,
    # and nothing to do with `git worktree`, which Tycho never uses). Every call here swallows
    # its stderr, so that refusal was silent and the shadow was simply never created.
    if _plain("init", "--quiet", "--bare", str(d)) != 0:
        return False
    if _run(repo, "config", "core.bare", "false")[0] != 0:
        return False
    try:
        info = d / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text(_EXCLUDE, encoding="utf-8")
    except OSError:
        pass
    # A first commit, so `HEAD` resolves from the very first turn. Without it the first turn
    # has no baseline and every check that needs one degrades exactly as it did before.
    commit(repo, "tycho: baseline")
    return True


def commit(repo: Path, message: str) -> str | None:
    """Snapshot the work tree. Returns the new sha, or None if git said no.

    `--allow-empty` on purpose: a turn that changed nothing is still a turn, and a missing
    commit would silently re-point the next turn's baseline at an older state.
    """
    if not exists(repo):
        return None
    if _run(repo, "add", "-A")[0] != 0:
        return None
    code, _ = _run(
        repo, "-c", "user.name=Tycho", "-c", "user.email=tycho@localhost",
        "commit", "--allow-empty", "--no-verify", "--quiet", "-m", message,
    )
    if code != 0:
        return None
    code, out = _run(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else None

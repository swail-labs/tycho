"""The `prepare-commit-msg` hook that stamps the `Tycho-Attestation:` trailer (§6.6).

The highest-risk file this package writes: not config, but code that runs inside every
`git commit`. Fenced by markers rather than owning the whole file, and it can never fail
the commit.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import spelling
from .configfile import _BACKUP_SUFFIX, ConfigRefused, _current_text, _drop_backup, _write_text


# The git hook that stamps the `Tycho-Attestation:` trailer (strategy §6.6). Fenced by
# markers rather than owning the whole file — a repo may already have its own hook.
_GIT_HOOK = "prepare-commit-msg"


_GIT_BEGIN = "# >>> tycho"


_GIT_END = "# <<< tycho"
# Tolerant of a hand-damaged fence: the closing marker is optional and the body matches only
# lines we write, so an unterminated block is repaired in place instead of stacking a second


# Tolerant of a hand-damaged fence: the closing marker is optional and the body matches only
# lines we write, so an unterminated block is repaired in place instead of stacking a second
# one, without eating the user's own lines below.
_GIT_BLOCK_RE = re.compile(
    rf"[ \t]*{re.escape(_GIT_BEGIN)}\b[^\n]*\n"
    rf"(?:(?![ \t]*# (?:>>>|<<<) tycho)[ \t]*"
    rf"(?:#[^\n]*Fails open[^\n]*|[^\n]*[Tt]ycho[^\n]*|[^\n]*\battest\b[^\n]*)\n)*"
    rf"(?:[ \t]*{re.escape(_GIT_END)}[^\n]*\n?)?"
)
# Hooks we'll append a `/bin/sh` block to. Anything else (python, a compiled binary, no
# shebang) is refused rather than rewritten.


# Hooks we'll append a `/bin/sh` block to. Anything else (python, a compiled binary, no
# shebang) is refused rather than rewritten.
_SH_SHEBANG = re.compile(r"^#!.*\b(sh|bash|dash|ash|zsh|ksh)\b")

# Per-repo config dir; created by the harness on first use, so its presence is a signal.


# --- the git prepare-commit-msg hook (the commit trailer, strategy §6.6) -----


def git_hooks_dir(repo: Path) -> Path | None:
    """This repo's hooks dir, or None when `repo` isn't a git repo (or git is absent). Uses
    `git rev-parse --git-path hooks`, not `<repo>/.git/hooks`: the only spelling correct for
    a plain repo, a linked worktree or submodule (`.git` is a *file*), and `core.hooksPath`.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return None  # no git on PATH — nothing to install into, and not an error
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = Path(proc.stdout.strip())
    # `--git-path` answers relative to the command's cwd, which `-C` set to `repo`.
    return path if path.is_absolute() else repo / path


def _hooks_dir_is_repo_local(repo: Path, hooks: Path) -> bool:
    """Is `hooks` inside this repo — its worktree or its git dir? A `core.hooksPath` set in
    the *global* config (`~/.githooks`) answers `--git-path hooks` with a directory shared by
    every repo on the machine, where a repo-local install has no business writing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return False
    target = Path(os.path.realpath(hooks))
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        root = Path(line.strip())
        root = Path(os.path.realpath(root if root.is_absolute() else repo / root))
        if target == root or root in target.parents:
            return True
    return False


def _git_hook_block() -> str:
    """Our fenced block: one line of work, wrapped so it cannot fail a commit. `|| :`
    swallows a non-zero exit even under a user's `set -e`; stdout/stderr are dropped so a
    traceback can't corrupt the message file; and the block never calls `exit`, so a foreign
    hook we sit in still runs every line. Inserted after the shebang rather than appended,
    since a foreign hook may exit early.

    `${1-}`/`${2-}`, not `$1`/`$2`: git leaves `$2` unset on a plain `git commit`, and under
    the `set -u` of an ordinary hand-written hook an unset expansion kills the shell *before*
    `|| :` is ever reached — the one way this block could refuse a commit."""
    return (
        f"{_GIT_BEGIN} — the Tycho-Attestation trailer. Remove with `tycho uninstall`.\n"
        f"# Fails open by construction: it can neither block nor fail this commit.\n"
        f'{spelling.attest_command()} "${{1-}}" "${{2-}}" >/dev/null 2>&1 || :\n'
        f"{_GIT_END}\n"
    )


def _make_executable(path: Path) -> None:
    """Git only runs a hook it can execute. No-op on Windows, where git goes by the shebang
    and `chmod` can't express the bit anyway."""
    try:
        mode = path.stat().st_mode
        path.chmod(mode | ((mode & 0o444) >> 2))  # +x wherever +r — i.e. `chmod +x` under umask
    except OSError:
        pass  # a hooks dir we can't chmod is a hook that won't fire — never a crash


def _refuse_shared_hook(repo: Path, path: Path) -> None:
    """Refuse a hook file that isn't this machine's to edit. Tracked (husky and friends keep
    `.husky/prepare-commit-msg` in the repo) means our absolute python path would be
    committed for the whole team; a symlink means the real edit lands somewhere else
    entirely, which for a hook is never what the user meant."""
    if path.is_symlink():
        raise ConfigRefused(
            f"{path} is a symlink — Tycho won't edit through it. Replace it with a real file "
            f"and re-run, or add "
            f'`{spelling.attest_command()} "${{1-}}" "${{2-}}" >/dev/null 2>&1 || :` to its target yourself'
        )
    try:
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
        ).returncode == 0
    except (OSError, ValueError):
        tracked = False
    if tracked:
        raise ConfigRefused(
            f"{path} is tracked by git — it's your team's file, and Tycho's line carries this "
            f"machine's python path. Add "
            f'`{spelling.attest_command()} "${{1-}}" "${{2-}}" >/dev/null 2>&1 || :` to it yourself, or '
            f"move the hook out of version control"
        )


def _install_git_hook(repo: Path) -> str | None:
    """Install/refresh the `prepare-commit-msg` hook. None when there's no repo. Our block
    is replaced in place, never stacked; an existing hook keeps every line; and a hook that
    isn't a `/bin/sh` script is left alone, since we can't reason about where a python or
    compiled hook tolerates an inserted line."""
    hooks = git_hooks_dir(repo)
    if hooks is None:
        return None
    if not _hooks_dir_is_repo_local(repo, hooks):
        raise ConfigRefused(
            f"core.hooksPath points outside this repo ({hooks}) — a hook written there would "
            f"run in every repo on this machine, and the next `tycho uninstall` would take it "
            f"from all of them. Point core.hooksPath inside the repo, or add "
            f'`{spelling.attest_command()} "${{1-}}" "${{2-}}" >/dev/null 2>&1 || :` to that hook yourself'
        )
    path = hooks / _GIT_HOOK
    _refuse_shared_hook(repo, path)
    current = _current_text(path)
    block = _git_hook_block()
    if current is None:
        text = f"#!/bin/sh\n{block}"
    elif _GIT_BLOCK_RE.search(current):
        text = _GIT_BLOCK_RE.sub(lambda _: block, current, count=1)
    elif not _SH_SHEBANG.match(current.splitlines()[0] if current.splitlines() else ""):
        raise ConfigRefused(
            f"{path} isn't a /bin/sh hook Tycho can extend safely — move it aside and re-run, "
            f"or add `{spelling.attest_command()} \"${{1-}}\" \"${{2-}}\" >/dev/null 2>&1 || :` to it yourself"
        )
    else:
        head, _, rest = current.partition("\n")
        text = f"{head}\n{block}{rest}"
    changed = _write_text(path, text, backup=current is not None)
    # Only ever chmod a hook that is ours: git doesn't run a non-executable hook at all, so
    # making a foreign one executable starts running code the user's git never ran.
    if current is None or _GIT_BLOCK_RE.sub("", text, count=1).strip() in ("", "#!/bin/sh"):
        _make_executable(path)
    if not changed:
        return f"git: commit trailer hook already current → {path}"
    verb = "installed" if current is None else "added the Tycho block to your"
    return f"git: {verb} {_GIT_HOOK} hook → {path}"


def _uninstall_git_hook(repo: Path) -> str | None:
    """Take our block back out. Restores the file byte-for-byte, or deletes it if it was
    ours — "ours" meaning nothing survives removal but the shebang we wrote."""
    hooks = git_hooks_dir(repo)
    if hooks is None:
        return None
    path = hooks / _GIT_HOOK
    current = _current_text(path)
    if current is None or not _GIT_BLOCK_RE.search(current):
        return None  # nothing of ours here — stay quiet rather than claim a removal
    remainder = _GIT_BLOCK_RE.sub("", current, count=1)
    if remainder.strip() in ("", "#!/bin/sh"):
        try:
            path.unlink()
            path.with_name(path.name + _BACKUP_SUFFIX).unlink(missing_ok=True)
        except OSError as exc:
            raise ConfigRefused(f"cannot remove {path} ({exc.strerror}) — fix that, then re-run") from exc
        return f"git: removed {_GIT_HOOK} hook → {path}"
    _write_text(path, remainder)
    _drop_backup(path)
    return f"git: removed the Tycho block from {path}"

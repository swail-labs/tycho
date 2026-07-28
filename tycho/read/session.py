"""Gather a session from the transcript, git and the filesystem.

`gather()` is the only inbound I/O boundary in Tycho: everything it returns is a frozen
`Session`, and everything downstream of it is pure. That is why this module lives in `read/`
and the checks live in `engine/`, which cannot import this package back.

The verdict half of the pipeline is re-exported below, so a caller that wants the whole
sequence still has one module to reach for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import os
from pathlib import Path, PurePosixPath

from ..engine import checks as checks_mod
from ..store import command as command_mod
from ..store import config as config_mod
from . import events as events_mod
from . import fsstate, gitstate
from ..store import state
from ..store.shadow import IGNORED_DIRS as _IGNORED_TEST_DIRS
from ..store.config import Config
from ..model import (
    Attribution,
    FileEdit,
    FileState,
    GitSnapshot,
    Session,
)


def gather(
    transcript: Path,
    repo: Path,
    config: Config | None = None,
    since: str = "HEAD",
    parse: Callable[[Path], tuple] | None = None,
    turn_start: Callable[[Path], float] | None = None,
    messages: Callable[[Path], tuple] | None = None,
    attribution: Callable[[Path], Attribution] | None = None,
) -> Session:
    """Read the transcript + config + file/git state into the immutable Session.

    ``parse`` selects the harness transcript reader (default: Claude Code). Omit
    ``turn_start`` to scope the whole transcript, which is what a manual audit wants."""
    events = (parse or events_mod.parse)(transcript)
    msgs = (messages or events_mod.assistant_messages)(transcript)
    # Transcripts record absolute paths; git and scope globs speak repo-relative. An edit
    # outside the repo stays absolute, so scope_drift flags it.
    edits = tuple(
        _with_baseline(replace(fe, path=_relpath(fe.path, repo)), repo, since)
        for fe in events_mod.file_edits(events)
    )
    files = {fe.path: _file_state(repo, fe.path) for fe in edits}
    start = turn_start(transcript) if turn_start else 0.0
    return Session(
        commands=command_mod.read(repo, since=_evidence_floor(events, edits, start)),
        events=events,
        edits=edits,
        repo=repo,
        config=config if config is not None else config_mod.load(repo),
        files=files,
        git=_git_snapshot(repo, since),
        has_tests=_has_tests(repo),
        turn_start=start,
        messages=msgs,
        attribution=attribution(transcript) if attribution else Attribution(),
    )


def _evidence_floor(events, edits, turn_start: float) -> float:
    """The earliest moment a `tycho exec` run may still count as evidence here.

    `commands.jsonl` is long-lived, so unbounded it offers yesterday's green `pytest` as proof
    of today's claim. The turn boundary, else the session's first timestamp, else 0.0 = admit
    nothing. Not an "older than N hours" rule — that makes the verdict depend on when you ran
    the verifier."""
    if turn_start:
        return turn_start
    stamps = [e.ts for e in events if e.ts] + [fe.ts for fe in edits if fe.ts]
    return min(stamps) if stamps else 0.0


# Test *naming* lives in `engine/checks/common.py`, and the directories not worth descending
# are the ones the shadow repo also refuses to record — both shared rather than restated here.


def _has_tests(repo: Path) -> bool:
    """Recognize conventional test files without walking dependencies or build output.

    A *yes* is cached in `.tycho/`: this runs every turn on the hot path and the answer changes
    once in a repo's life (a 4,000-file Rust tree re-read 76 MB each time). Only the yes — a
    stale "no tests" disables the whole test-check family the moment the first test lands.
    """
    marker = state.dir_for(repo) / _HAS_TESTS_MARKER
    try:
        if marker.exists():
            return True
    except OSError:
        pass
    if _walk_for_tests(repo):
        try:
            if marker.parent.is_dir():  # only where Tycho is installed
                marker.touch()
        except OSError:
            pass
        return True
    return False


_HAS_TESTS_MARKER = "has-tests"


def _walk_for_tests(repo: Path) -> bool:
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _IGNORED_TEST_DIRS and not d.startswith(".")]
        try:
            rel = Path(root).relative_to(repo).as_posix()
        except ValueError:
            rel = ""
        for name in files:
            if checks_mod._is_test_path(f"{rel}/{name}" if rel and rel != "." else name):
                return True
            # Rust names tests by attribute, not by path — the only convention that needs the
            # file's contents, so it cannot live in the path predicate.
            if Path(name).suffix == ".rs" and name != "build.rs":
                try:
                    text = (Path(root) / name).read_text(encoding="utf-8", errors="ignore")
                    if any(marker in text for marker in ("#[test]", "#[rstest]", "proptest!")):
                        return True
                except OSError:
                    pass
    return False


def _with_baseline(fe: FileEdit, repo: Path, since: str) -> FileEdit:
    """Recover the pre-edit baseline from git when the harness omitted it. Claude Code sends
    ``originalFile: null`` on repeat edits, leaving the AST checks silently UNSUPPORTED."""
    if fe.original is not None or not checks_mod._is_in_repo(fe.path):
        return fe
    blob = gitstate.blob_at(repo, since, fe.path)
    if blob is None:
        return fe
    return replace(fe, original=blob, kind="edit")


def _relpath(path: str, repo: Path) -> str:
    """Repo-relative, always forward slashes. Git and the `[scope]` globs both speak POSIX, so
    a Windows backslash reconciles against neither."""
    # `is_absolute()` only knows the *host* flavor: on Windows `/repo/old.md` (no drive) reads
    # as relative, and WSL emits exactly that.
    p = Path(path)
    if p.is_absolute():
        try:
            return p.relative_to(repo).as_posix()
        except ValueError:
            return path  # edit outside the repo — keep absolute so scope_drift catches it
    pp = PurePosixPath(path)
    if pp.is_absolute():
        try:
            return pp.relative_to(PurePosixPath(repo.as_posix())).as_posix()
        except ValueError:
            return path
    return path.replace("\\", "/")


def _file_state(repo: Path, rel_path: str) -> FileState:
    abs_path = repo / rel_path
    text = None
    if fsstate.exists(abs_path):
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
    return FileState(
        path=rel_path,
        exists=fsstate.exists(abs_path),
        mtime=fsstate.mtime(abs_path),
        current_text=text,
    )


def _git_snapshot(repo: Path, since: str) -> GitSnapshot:
    if not gitstate.is_repo(repo):
        return GitSnapshot(is_repo=False, head_sha=None, changed_paths=())
    return GitSnapshot(
        is_repo=True,
        head_sha=gitstate.head_sha(repo),
        changed_paths=gitstate.diff_names(repo, since),
    )


from ..engine import has_verifiable_activity as has_verifiable_activity  # noqa: E402
from ..engine import run_checks as run_checks  # noqa: E402
from ..engine import verdict_of as verdict_of  # noqa: E402
from ..engine.verdict import _SUBSTANTIVE_CHECKS as _SUBSTANTIVE_CHECKS  # noqa: E402

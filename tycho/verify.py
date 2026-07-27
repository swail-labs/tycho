"""The engine: gather a session, run the checks, reduce their results to a verdict.

`gather()` is the only I/O boundary — everything downstream is pure over a frozen `Session`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
import os
from pathlib import Path, PurePosixPath

from . import checks as checks_mod
from . import command as command_mod
from . import config as config_mod
from . import events as events_mod
from . import fsstate, gitstate
from .config import Config
from .model import (
    Attribution,
    CheckResult,
    CheckStatus,
    FileEdit,
    FileState,
    GitSnapshot,
    Session,
    Verdict,
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
    # Harness transcripts record absolute paths; git and scope globs speak repo-relative.
    # Normalize once here (an edit outside the repo stays absolute — scope_drift flags it).
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
    """The earliest moment a `tycho exec` run may still count as evidence for this session.

    `.tycho/commands.jsonl` is repo-scoped and long-lived, so read unbounded it would offer
    yesterday's green `pytest` as proof of today's claim. The floor is the turn boundary,
    else the session's first timestamp, else **0.0 meaning admit nothing** (`command.read`
    returns () for 0.0). Deliberately *not* an "older than N hours" rule: that would make
    the verdict depend on when you ran the verifier."""
    if turn_start:
        return turn_start
    stamps = [e.ts for e in events if e.ts] + [fe.ts for fe in edits if fe.ts]
    return min(stamps) if stamps else 0.0


_IGNORED_TEST_DIRS = {".git", ".venv", "venv", "node_modules", "vendor", "target", "dist", "build"}
_TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__"}
_SUFFIX_TEST_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".dart", ".go", ".java", ".js", ".jsx",
    ".kt", ".kts", ".m", ".mm", ".php", ".py", ".rb", ".swift", ".ts", ".tsx",
}


def _has_tests(repo: Path) -> bool:
    """Recognize conventional test files without walking dependencies or build output."""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _IGNORED_TEST_DIRS and not d.startswith(".")]
        try:
            in_test_dir = any(part.lower() in _TEST_DIRS for part in Path(root).relative_to(repo).parts)
        except ValueError:
            in_test_dir = False
        for name in files:
            stem, suffix = Path(name).stem, Path(name).suffix
            if in_test_dir or name == "conftest.py" or stem.startswith("test_"):
                return True
            if stem.endswith(("_test", "_spec")) or ".test." in name or ".spec." in name:
                return True
            if suffix.lower() in _SUFFIX_TEST_EXTENSIONS and stem.endswith(("Test", "Tests", "Spec")):
                return True
            if suffix == ".rs" and name != "build.rs":
                try:
                    text = (Path(root) / name).read_text(errors="ignore")
                    if any(marker in text for marker in ("#[test]", "#[rstest]", "proptest!")):
                        return True
                except OSError:
                    pass
    return False


def _with_baseline(fe: FileEdit, repo: Path, since: str) -> FileEdit:
    """Recover the pre-edit baseline from git when the harness omitted it: Claude Code sends
    ``originalFile: null`` on a file's *repeat* edits, which would leave the AST tamper
    checks with no "before" and silently UNSUPPORTED while appearing to run."""
    if fe.original is not None or not checks_mod._is_in_repo(fe.path):
        return fe
    blob = gitstate.blob_at(repo, since, fe.path)
    if blob is None:
        return fe
    return replace(fe, original=blob, kind="edit")


def _relpath(path: str, repo: Path) -> str:
    """Make an absolute in-repo path repo-relative, always with forward slashes.

    Git and the `[scope]` globs both speak POSIX separators, so a Windows backslash here
    would reconcile against neither — `git_state` would count zero uncommitted and
    `scope_drift` would miss `src/**`."""
    # `is_absolute()` only knows the *host* flavor: on Windows a POSIX path like
    # `/repo/old.md` (no drive) reads as relative, and WSL emits exactly that.
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


# file_state/git_state only establish that the edited files exist and are in the repo,
# which is true of any session that touched a file — alone they'd turn "I couldn't check
# anything" into a green VERIFIED. They corroborate a claim; they can't carry one.
_WEAK_CHECKS = frozenset({"file_state", "git_state"})


def verdict_of(results: Sequence[CheckResult]) -> Verdict:
    """Reduce per-check statuses to one run verdict.

    Any FAIL sinks the run; else any STALE; else one *substantive* PASS verifies (a
    `_WEAK_CHECKS` pass alone does not). Nothing conclusive: all-UNSUPPORTED ->
    UNSUPPORTED, otherwise INDETERMINATE (including the empty case)."""
    statuses = {r.status for r in results}
    if CheckStatus.FAIL in statuses:
        return Verdict.FAILED
    if CheckStatus.STALE in statuses:
        return Verdict.STALE
    if any(r.status is CheckStatus.PASS and r.name not in _WEAK_CHECKS for r in results):
        return Verdict.VERIFIED
    if statuses and statuses <= {CheckStatus.UNSUPPORTED}:
        return Verdict.UNSUPPORTED
    return Verdict.INDETERMINATE


def run_checks(session: Session) -> list[CheckResult]:
    """Run the enabled checks over the gathered snapshot (pure)."""
    return checks_mod.run_checks(session)


def has_verifiable_activity(session: Session) -> bool:
    """True when a Stop hook has edits or a recognized test/build command."""
    return checks_mod.has_verifiable_activity(session)

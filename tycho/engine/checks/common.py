"""Small helpers every check shares: how a result is built, what the evidence line calls
its scope, and which bucket a path falls in."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..model import CheckResult, CheckStatus, Session


# --- helpers ----------------------------------------------------------------

def _r(name: str, status: CheckStatus, evidence: str) -> CheckResult:
    return CheckResult(name, status, evidence)


def _scope(session: Session) -> str:
    """What the evidence line calls its scope. No turn boundary means "session"."""
    return "turn" if session.turn_start else "session"


def _short(cmd: str, limit: int = 50) -> str:
    cmd = cmd.strip().splitlines()[0] if cmd.strip() else cmd
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"


# Editing these can't invalidate a green run, and STALE sinks the whole verdict. Narrow on
# purpose — config and lockfiles stay sources; a dependency change can break tests.
_PROSE_SUFFIXES = frozenset({
    ".md", ".rst", ".txt", ".adoc",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf",
})


_PROSE_NAMES = frozenset({"LICENSE", "NOTICE", "AUTHORS", "CODEOWNERS"})


def _is_prose_path(path: str) -> bool:
    """True for a file whose edits can't change what a test run proves."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base in _PROSE_NAMES:
        return True
    dot = base.rfind(".")
    return dot > 0 and base[dot:].lower() in _PROSE_SUFFIXES


def _is_source_path(path: str) -> bool:
    """True for a file a test run actually covers — not a test, not prose."""
    return not _is_test_path(path) and not _is_prose_path(path)


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{p}"
        or p.startswith("tests/")
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
    )


def _is_in_repo(path: str) -> bool:
    """True for a path `_relpath` left repo-relative. Out-of-repo edits stay absolute in
    native *or* POSIX flavor, so both are tested."""
    return not Path(path).is_absolute() and not PurePosixPath(path).is_absolute()

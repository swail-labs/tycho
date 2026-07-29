"""Small helpers every check shares: how a result is built, what the evidence line calls
its scope, and which bucket a path falls in."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .. import textdiff
from ...model import CheckResult, CheckStatus, Session


# --- helpers ----------------------------------------------------------------

def _r(name: str, status: CheckStatus, evidence: str, blocking: bool = False) -> CheckResult:
    return CheckResult(name, status, evidence, blocking)


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

# The comment above says lockfiles stay sources, and for `poetry.lock` and `go.sum` it was
# true — but Python's dependency manifests end in `.txt`, so `_PROSE_SUFFIXES` swallowed the
# most common one there is. Editing `requirements.txt` after a green run could not go STALE,
# which is exactly the dependency change the comment promises to catch.
_DEPENDENCY_NAMES = frozenset({
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "constraints.txt", "dev-requirements.txt", "test-requirements.txt",
})


def _is_prose_path(path: str) -> bool:
    """True for a file whose edits can't change what a test run proves."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower() in _DEPENDENCY_NAMES:
        return False
    if base in _PROSE_NAMES:
        return True
    dot = base.rfind(".")
    return dot > 0 and base[dot:].lower() in _PROSE_SUFFIXES


def _is_source_path(path: str) -> bool:
    """True for a file *this repo's* test run actually covers — not a test, not prose, and
    not outside the repo.

    The repo bound is load-bearing, not tidiness. `_relpath` deliberately leaves an
    out-of-repo edit absolute so `scope_drift` can flag it, and `_file_state` then resolves
    that absolute path to a real mtime — so without this, an agent that touched a scratch
    file in /tmp pinned the repo STALE, citing "edited 195s after the last passing test run"
    about a file the suite was never going to cover. Caught on Tycho's own repo. Editing
    outside the repo is `scope_drift`'s to report; it is not staleness.
    """
    return _is_in_repo(path) and not _is_test_path(path) and not _is_prose_path(path)


# The naming conventions that make a file a test. Deliberately the same vocabulary the repo
# scan uses (`read/session.py:_walk_for_tests`, which calls this): a language whose files count
# as "this repo has tests" must be a language whose test edits the checks can see. When these
# disagreed, `slug.test.js` enabled the test-check family and then read as a source file — so
# `assertion_weakening`, `skip_mock_injection` and `test_provenance` reported "no edited test
# files to diff" on every JS/TS/Go/Java repo, and an agent editing a red test green went
# unverified.
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})

# Suffix-only conventions (`FooTest.java`) need the extension too — `Manifest` is not a test.
_TEST_SUFFIX_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".dart", ".go", ".java", ".js", ".jsx",
    ".kt", ".kts", ".m", ".mm", ".php", ".py", ".rb", ".swift", ".ts", ".tsx",
})


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    # Prose first: `docs/spec/format.md` lives under a directory named `spec`, but a markdown
    # file is not a test whatever directory holds it. Read as one, it joined the test-edit set
    # and `assertion_weakening` went looking for assertions to diff in a changelog.
    if _is_prose_path(p):
        return False
    if any(part.lower() in _TEST_DIRS for part in p.split("/")[:-1]):
        return True
    if base == "conftest.py":
        return True
    name = PurePosixPath(base)
    stem, suffix = name.stem, name.suffix.lower()
    return (
        stem.startswith("test_")
        or stem.endswith(("_test", "_spec"))
        or ".test." in base
        or ".spec." in base
        or (suffix in _TEST_SUFFIX_EXTENSIONS and stem.endswith(("Test", "Tests", "Spec")))
    )


def _is_test_edit(fe, session) -> bool:
    """Is this edit an edit to a test? Path convention first, then the file's own contents.

    Content matters because several ecosystems colocate: Rust's `#[cfg(test)] mod tests` lives
    in `src/lib.rs`, Go allows a test in any `.go` file, and a Python `test_` function can sit
    anywhere. Path-only, those tests were invisible to `test_provenance` and the tamper checks
    while the repo scan — which *does* read contents — counted the same file as proof the repo
    has tests.

    Note this does not make the file stop being a *source* file: `src/lib.rs` is both, and
    `_is_source_path` stays path-based so editing it after a green run is still STALE.
    """
    if not _is_in_repo(fe.path):
        return False  # someone else's test file — this repo's runner never saw it
    if _is_test_path(fe.path):
        return True
    fs = session.files.get(fe.path) if session is not None else None
    return textdiff.contains_tests(fe.path, (fs.current_text if fs else None) or fe.original)


def _shell_written_tests(session) -> list[str]:
    """Test files this session wrote with the shell instead of the Write/Edit tools.

    `session.edits` is built from those two tools alone, so `cat > tests/test_x.py <<'EOF'`
    produced a test file that no check could see. The three test checks then reported "no test
    files touched", non-blocking — a false statement that let `command_execution` mint VERIFIED
    on its own, over a test nothing had inspected.

    There is no baseline for a file written this way, so this cannot produce a diff. It exists
    to turn a wrong confident answer into an honest one.
    """
    from .cmdread import _SHELL_TOOLS, written_paths

    seen = []
    for e in session.events:
        if e.tool not in _SHELL_TOOLS:
            continue
        for path in written_paths(e.input.get("command") or ""):
            rel = path.lstrip("./")
            if _is_test_path(rel) and rel not in seen:
                seen.append(rel)
    return seen


def _is_in_repo(path: str) -> bool:
    """True for a path `_relpath` left repo-relative. Out-of-repo edits stay absolute in
    native *or* POSIX flavor, so both are tested."""
    return not Path(path).is_absolute() and not PurePosixPath(path).is_absolute()

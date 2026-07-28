"""`assertion_weakening` and `skip_mock_injection` — was the green bought by editing the
test rather than fixing the code?

Two readers, picked per file. Python goes through `astdiff`, which parses and is exact.
Everything else goes through `textdiff`, which counts test cases, assertions and skip markers
and reports only a decrease (or, for skips, an increase). A file neither can read is reported
as unread — never as clean, and never as a green another check can paper over.
"""

from __future__ import annotations

from .. import textdiff
from ..astdiff import assertion_delta, parseable, skip_or_mock_added
from ...model import CheckResult, CheckStatus, Session
from .common import _is_test_edit, _r, _shell_written_tests


def assertion_weakening(session: Session) -> CheckResult:
    return _tamper_check(session, "assertion_weakening", assertion_delta,
                         textdiff.assertion_delta,
                         "no assertions removed or weakened in edited tests")


def skip_mock_injection(session: Session) -> CheckResult:
    return _tamper_check(session, "skip_mock_injection", skip_or_mock_added,
                         textdiff.skip_or_mock_added,
                         "no skips or mocks injected into edited tests")


def _is_python(path: str) -> bool:
    return path.rsplit(".", 1)[-1].lower() in ("py", "pyi") if "." in path else False


def _tamper_check(session: Session, name: str, ast_differ, text_differ, clean_msg: str) -> CheckResult:
    test_edits = [fe for fe in session.edits if _is_test_edit(fe, session)]
    if not test_edits:
        # A test written by shell redirect leaves no before/after to diff — the same capability
        # gap the `firsts` branch below blocks on, arriving one step earlier.
        if written := _shell_written_tests(session):
            return _r(
                name, CheckStatus.UNSUPPORTED,
                f"{written[0]} was written by a shell command, not an edit tool — no baseline "
                f"exists to diff, so Tycho can't tell whether its assertions survived",
                blocking=True,
            )
        return _r(name, CheckStatus.UNSUPPORTED, "no edited test files to diff")
    # earliest original per path = the file before the session's first edit
    firsts: dict[str, str] = {}
    for fe in sorted(test_edits, key=lambda e: e.ts):
        if fe.original is not None:
            firsts.setdefault(fe.path, fe.original)
    if not firsts:
        # Edited, but no pre-session baseline to diff against: a capability gap, not an
        # all-clear.
        missing = ", ".join(sorted({fe.path for fe in test_edits}))
        return _r(name, CheckStatus.UNSUPPORTED,
                  f"edited test file(s) with no pre-session baseline to diff: {missing}",
                  blocking=True)
    findings, unread = [], []
    for path, before in firsts.items():
        fs = session.files.get(path)
        after = fs.current_text if fs else None
        # Dispatch on the extension, not on whether `ast` happens to accept the bytes: Python's
        # parser reads plenty of other languages' snippets without complaint (`assert x == 1`
        # is valid Elixir *and* valid Python), which would silently apply Python semantics to
        # a file that is not Python.
        if _is_python(path):
            if parseable(before) and parseable(after):
                findings.extend(f"{path}: {f}" for f in ast_differ(before, after))
            else:
                unread.append(path)  # Python that does not parse — mid-edit or a syntax error
        elif textdiff.supported(path):
            findings.extend(f"{path}: {f}" for f in text_differ(path, before, after))
        else:
            unread.append(path)
    if findings:
        return _r(name, CheckStatus.FAIL, "; ".join(findings))
    # A file neither reader knows is a gap, not an all-clear — and `blocking` keeps another
    # check's PASS from minting a green over it. A Sonnet session deleted a failing Go test
    # outright and the turn came back VERIFIED with "nothing open" when this said nothing.
    if unread:
        return _r(name, CheckStatus.UNSUPPORTED,
                  f"no reader for this file type: {', '.join(sorted(unread))}",
                  blocking=True)
    return _r(name, CheckStatus.PASS, clean_msg)

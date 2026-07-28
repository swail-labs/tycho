"""`test_freshness` and `test_provenance` — does the last passing run still cover the tree?

Both reason across turns, which is why they read the whole session rather than the turn.
"""

from __future__ import annotations

from ...model import CheckResult, CheckStatus, Session
from .common import _is_source_path, _is_test_edit, _r
from .outcome import _last_green_run_ts


def test_freshness(session: Session) -> CheckResult:
    green_ts = _last_green_run_ts(session)
    if green_ts is None:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no passing test run to check against")
    source_edits = [fe for fe in session.edits if _is_source_path(fe.path)]
    if not source_edits:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no source edits to check against the run")
    horizon = _clock_horizon(session)
    stale, skewed = [], []
    for fe in source_edits:
        fs = session.files.get(fe.path)
        if not (fs and fs.mtime is not None):
            continue
        if fs.mtime > horizon:
            skewed.append(fe.path)
        elif fs.mtime > green_ts:
            stale.append((fe.path, fs.mtime))
    if stale:
        path, mt = max(stale, key=lambda x: x[1])
        # Session-scoped: an uncovered source from an earlier turn still counts, but the
        # wording must not imply a this-turn edit.
        if path in {fe.path for fe in session.turn_edits}:
            evidence = f"{path} edited {int(mt - green_ts)}s after the last passing test run"
        else:
            evidence = f"{path} still uncovered since the last passing run (last edited in an earlier turn)"
        return _r("test_freshness", CheckStatus.STALE, evidence)
    if skewed:
        return _r(
            "test_freshness",
            CheckStatus.UNSUPPORTED,
            f"{sorted(skewed)[0]} has an mtime in the future (clock skew, or a copied/extracted"
            " file) — Tycho can't tell whether it postdates the last passing run",
        )
    return _r("test_freshness", CheckStatus.PASS, "sources unchanged since the last passing run")


# An mtime this far past everything the session recorded came from a tarball or a skewed
# clock, not an edit — read as one it pins the repo at STALE forever. Measured against the
# session's own latest timestamp, never the wall clock, so checks stay pure.
_CLOCK_SLACK = 3600.0


def _clock_horizon(session: Session) -> float:
    stamps = [e.ts for e in session.events]
    stamps += [fe.ts for fe in session.edits]
    stamps += [m.ts for m in session.messages]
    stamps += [c.ended_at for c in session.commands]
    return max(stamps, default=0.0) + _CLOCK_SLACK


def test_provenance(session: Session) -> CheckResult:
    test_edits = [fe for fe in session.edits if _is_test_edit(fe, session)]
    if not test_edits:
        return _r("test_provenance", CheckStatus.UNSUPPORTED, "no test files touched this session")
    green_ts = _last_green_run_ts(session)
    if green_ts is None:
        return _r("test_provenance", CheckStatus.UNSUPPORTED, "no passing test run to check against")
    after = [(fe.path, fe.ts) for fe in test_edits if fe.ts > green_ts]
    if after:
        path, ts = max(after, key=lambda x: x[1])
        return _r(
            "test_provenance",
            CheckStatus.FAIL,
            f"{path} modified {int(ts - green_ts)}s after the last passing run — that run didn't cover it",
        )
    return _r("test_provenance", CheckStatus.PASS, "tests unchanged since the last passing run")

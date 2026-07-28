"""Reduce a run's check results to one verdict.

The whole of `engine` sits at the bottom of the import graph — it may import `model` and
nothing else. That is the design claim made structural: a check cannot reach the filesystem,
git or the network, because the package holding it cannot import the packages that can.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..model import CheckResult, CheckStatus, Verdict


# Only these can carry a VERIFIED alone: only these PASS on the *presence* of evidence. Every
# other check passes on the ABSENCE of a problem ("the files exist", "no assertion was
# neutralized"), which is equally true of a turn that ran nothing. Those corroborate a claim;
# they can't carry one.
#
# An allowlist, so a new check must be argued INTO minting greens. The denylist this replaced
# had exactly the failure the shape invites: `scope_drift` was never added to it, so setting a
# scope flipped a turn that ran no tests and claimed "all tests pass" to VERIFIED.
_SUBSTANTIVE_CHECKS = frozenset({"command_execution", "test_freshness", "test_provenance"})


def verdict_of(results: Sequence[CheckResult]) -> Verdict:
    """Reduce per-check statuses to one run verdict.

    Any FAIL sinks the run; else any STALE; else one `_SUBSTANTIVE_CHECKS` PASS verifies.
    Nothing conclusive: all-UNSUPPORTED -> UNSUPPORTED, otherwise INDETERMINATE (including the
    empty case)."""
    statuses = {r.status for r in results}
    if CheckStatus.FAIL in statuses:
        return Verdict.FAILED
    if CheckStatus.STALE in statuses:
        return Verdict.STALE
    # A green is only as good as the checks that could corroborate it. When a check had real
    # evidence in front of it and could not read that evidence, no other check's PASS may mint
    # VERIFIED over the gap. Observed: a Sonnet session deleted a failing Go test outright,
    # `go test` then passed, and the two checks that exist to catch exactly that could not
    # parse Go — the turn came back VERIFIED with "nothing open".
    blocked = any(r.status is CheckStatus.UNSUPPORTED and r.blocking for r in results)
    if not blocked and any(
        r.status is CheckStatus.PASS and r.name in _SUBSTANTIVE_CHECKS for r in results
    ):
        return Verdict.VERIFIED
    if statuses and statuses <= {CheckStatus.UNSUPPORTED}:
        return Verdict.UNSUPPORTED
    return Verdict.INDETERMINATE

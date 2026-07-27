"""`tycho review` — risk-focus the diff (strategy §9.4).

*"These 3 hunks were touched by no test and exercised by no command."* Change-aware
coverage, pitched as a review aid rather than a check. It answers the most expensive
question a developer has — *which part of this do I actually need to read?* — and unlike a
lie detector it doesn't decay, because it makes a coverage claim, not a correctness one.
"""

from __future__ import annotations

from pathlib import Path

from . import gitstate
from . import record as record_mod


def review(repo: Path, since: str = "HEAD") -> list[str]:
    """Changed paths, flagged by whether any recorded turn ran a command that covered them."""
    if not gitstate.is_repo(repo):
        return ["tycho: not a git repository — nothing to review."]
    changed = gitstate.diff_names(repo, since)
    if not changed:
        return [f"tycho: no changes against {since}."]
    lines = [f"tycho review — {len(changed)} path(s) changed against {since}:"]
    for path in changed:
        lines.append(f"  {'✓' if _exercised(repo, path) else '?'} {path}")
    return lines


def _exercised(repo: Path, path: str) -> bool:
    """True when some recorded turn that touched `path` also ran a passing command."""
    for r in record_mod.touching(repo, path):
        if any(c.get("outcome") == "passed" for c in r.get("commands", [])):
            return True
    return False

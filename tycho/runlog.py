"""Read a test runner's own verdict out of its terminal output — the fallback for when the
exit code isn't available. Failure is checked before success because a red summary names
both ("1 failed, 76 passed in 1.07s").

`.pytest_cache/v/cache/lastfailed` looks like a better source and is not: pytest only
*removes* an entry when that test runs and passes, so deselected/renamed/deleted tests
persist forever. Measured here, it named a test that no longer exists and survived a fully
green 316-test run untouched. Trusting it would report FAILED on a green repo.
"""

from __future__ import annotations

import re

# [1-9] not \d: "0 failed" is a runner reporting success.
_FAILURE = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b|=+ ERRORS =+")

# The duration is required so a stray "5 passed" in prose isn't read as a run.
_SUCCESS = re.compile(r"\b\d+ passed\b.*?\bin \d+(?:\.\d+)?s\b", re.DOTALL)


def outcome(text: str) -> bool | None:
    """True = runner reported failures, False = success, None = no recognized summary.

    None is the common case: no summary means no verdict, never an assumed pass.
    """
    if not text:
        return None
    if _FAILURE.search(text):
        return True
    if _SUCCESS.search(text):
        return False
    return None

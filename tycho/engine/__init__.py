"""The verdict: pure functions over a frozen `Session`.

    checks/   the deterministic checks, one module per family
    verdict   reduce their results to one run verdict
    astdiff   AST diffs of an edited test file
    runlog    read a runner's own summary out of its output

Nothing here does I/O. `read/` gathers the Session and calls in; the arrow never points back
out, which `test_the_engine_imports_nothing_that_does_io` enforces.
"""

from __future__ import annotations

from . import checks
from .checks import has_verifiable_activity, run_checks
from .verdict import _SUBSTANTIVE_CHECKS, verdict_of

__all__ = ["checks", "has_verifiable_activity", "run_checks", "verdict_of", "_SUBSTANTIVE_CHECKS"]

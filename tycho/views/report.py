"""Render a verdict + its check results as the terminal block.

Callers: ``tycho verify`` (human asked — verbose) and the relay (``hook._relay_output``).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..model import CheckResult, CheckStatus, Verdict

_MARK = {
    CheckStatus.PASS: "✓",
    CheckStatus.FAIL: "✗",
    CheckStatus.STALE: "✗",
    CheckStatus.UNSUPPORTED: "•",
    CheckStatus.INDETERMINATE: "?",
}

# The model-facing relay copy shows only these; the human channel still shows every line.
_ADVERSE = frozenset({CheckStatus.FAIL, CheckStatus.STALE, CheckStatus.INDETERMINATE})


def render(
    verdict: Verdict,
    results: Sequence[CheckResult],
    claim: str | None = None,
    only_adverse: bool = False,
) -> str:
    """One header line + one line per check (mark, name, evidence).

    ``claim`` is echoed verbatim, never parsed or semantically matched. ``only_adverse``
    keeps just the FAIL/STALE/INDETERMINATE lines, falling back to all when none are adverse.
    """
    header = f"🔍 Tycho: {verdict}"
    if verdict is Verdict.OVERRIDDEN:
        header += " — agent-authorized (not proven)"
    lines = [header]
    if claim:
        lines.insert(0, f'   claim: "{claim}"')
    shown = [r for r in results if r.status in _ADVERSE] if only_adverse else list(results)
    if not shown:
        shown = list(results)  # nothing to single out — don't leave the header bare
    lines.extend(f"  {_MARK[r.status]} {r.name} — {r.evidence}" for r in shown)
    return "\n".join(lines)

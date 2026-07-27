"""Render a verdict + its check results as the terminal block.

Since the turn digest took over the Stop hook's human channel (strategy §9.1), this is no
longer the unprompted output. It has two remaining callers, and both *want* every line:

- ``tycho verify`` — the human explicitly asked, so selectivity doesn't apply. Verbose stays.
- the relay (``hook._relay_output``) — the adverse-only copy for the model, and the full copy
  on the `systemMessage` that accompanies it.

Not deprecated, and deliberately unchanged: "what did all nine checks say?" is a real question,
just not one worth answering after every turn unasked.
"""

from __future__ import annotations

from collections.abc import Sequence

from .model import CheckResult, CheckStatus, Verdict

_MARK = {
    CheckStatus.PASS: "✓",
    CheckStatus.FAIL: "✗",
    CheckStatus.STALE: "✗",
    CheckStatus.UNSUPPORTED: "•",
    CheckStatus.INDETERMINATE: "?",
}

# The checks a relay re-check exists to act on. The model-facing copy shows only these so it
# isn't a second full transcript of the verdict the human already sees (the harness renders
# `additionalContext` verbatim as "Stop hook feedback"). The human channel still shows every line.
_ADVERSE = frozenset({CheckStatus.FAIL, CheckStatus.STALE, CheckStatus.INDETERMINATE})


def render(
    verdict: Verdict,
    results: Sequence[CheckResult],
    claim: str | None = None,
    only_adverse: bool = False,
) -> str:
    """One header line + one line per check (mark, name, evidence).

    ``claim`` (from ``tycho verify --claim``) is echoed verbatim above the verdict so
    the human can compare what was *said* against what was *proven*. Tycho does not
    parse or semantically match the claim — structured claim matching is a Pro concern.

    ``only_adverse`` keeps just the FAIL/STALE/INDETERMINATE lines — the model-facing relay
    copy, so it names what to fix without re-listing every passing check (TYCHO-116 follow-up:
    the harness echoes `additionalContext`, so a full copy there duplicates the human's verdict).
    Falls back to all lines when nothing is adverse, so the header never stands alone.
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

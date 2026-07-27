"""The turn digest: a receipt of what the turn did, not a report card for our checks.

Strategy §6.1/§9.1. `report.render` answers *"did it lie?"* — a question asked rarely, and
answered VERIFIED nearly every time, so by turn 20 nobody reads it. The digest answers the
questions asked after every turn: what changed, what ran, what was claimed, what is still
unverified. The verdict becomes one field, not the headline.

**Selectivity is the feature, not a nicety** (§11.1). A fixed-shape summary after every turn
is wallpaper by day three. `speaks()` decides whether an unprompted digest is worth a
developer's attention this turn; the full digest is always available on demand.
"""

from __future__ import annotations

from .model import Stage, Verdict

# Verdicts worth interrupting for. VERIFIED/UNSUPPORTED are the routine outcome — the whole
# point of §11.1 is that they earn silence.
_ADVERSE = frozenset({Verdict.FAILED.name, Verdict.STALE.name, Verdict.OVERRIDDEN.name})


def speaks(record: dict) -> bool:
    """Whether this turn is worth an unprompted digest. Silence is the default."""
    return record.get("verdict") in _ADVERSE


def render(record: dict) -> str:
    """The full digest for one turn record — the on-demand view (`tycho show`)."""
    lines = [f"🔍 Tycho — turn {record.get('id', '?')} · {record.get('verdict', '?')}"]
    stage = record.get("stage")
    if stage:
        lines.append(f"  ladder: {_ladder(stage)}")
    for f in record.get("files", []):
        lines.append(f"  {f.get('kind', 'edit')}: {f.get('path')}")
    for c in record.get("commands", []):
        lines.append(f"  ran: {c.get('cmd')} — {c.get('outcome')}")
    for c in record.get("checks", []):
        if c.get("status") not in ("PASS", "UNSUPPORTED"):
            lines.append(f"  {c.get('name')}: {c.get('evidence')}")
    return "\n".join(lines)


def _ladder(stage: str) -> str:
    """`attempted → executed → artifact_changed` with the reached rung marked."""
    rungs = [s.value for s in Stage]
    try:
        reached = rungs.index(stage)
    except ValueError:
        return stage
    return " → ".join(r.upper() if i == reached else r for i, r in enumerate(rungs[: reached + 1]))

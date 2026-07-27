"""`tycho blame` and `tycho log` — agent archaeology over the turn record (strategy §9.3).

Git tells you what changed. This tells you *what the agent said it was doing* and what
evidence backed it. The only answer available today is unreadable JSONL buried in
`~/.claude/projects`, which is to say: no answer.

`blame` before `log` on purpose (§11.3): it rides muscle memory people already have and
lands *inside* the debugging moment, where `log` is a destination you have to remember.
"""

from __future__ import annotations

from pathlib import Path

from . import record as record_mod


def blame(repo: Path, target: str, limit: int = 10) -> list[str]:
    """Which turns touched `target`, newest first, with the claim and the evidence."""
    path = target.split(":", 1)[0]
    records = record_mod.touching(repo, path, limit=limit)
    if not records:
        return [f"tycho: no recorded turn has touched {path}."]
    return [f"{_when(r)} — {r.get('verdict')} — {_claim(r)}" for r in records]


def log(repo: Path, limit: int = 20) -> list[str]:
    """The last `limit` turns, newest first — the history view."""
    records = record_mod.read(repo, limit=limit)
    if not records:
        return ["tycho: no turns recorded yet."]
    return [f"{_when(r)} {r.get('verdict'):<13} {_claim(r)}" for r in records]


def _claim(record: dict) -> str:
    claims = record.get("claims") or []
    return claims[0].splitlines()[0][:100] if claims else "(no claim recorded)"


def _when(record: dict) -> str:
    from datetime import datetime

    ts = record.get("ended_at") or 0
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"

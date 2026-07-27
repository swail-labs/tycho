"""The commit trailer: `Tycho-Attestation: sha256:…` (strategy §9.7/§6.6).

Cheap, rides git, permanent, `git log`-visible, and portable without a PR gate. Solo-useful
six months later: it tells you which commits were agent-written and never verified by
anything. The digest is `record.digest` over the canonical record, so the attestation is
reproducible from the record itself rather than asserted.
"""

from __future__ import annotations

from pathlib import Path

from . import record as record_mod

TRAILER = "Tycho-Attestation"


def trailer(repo: Path) -> str | None:
    """The trailer line for the most recent recorded turn, or None when there is none."""
    latest = record_mod.read(repo, limit=1)
    if not latest:
        return None
    return f"{TRAILER}: {record_mod.digest(latest[0])}"

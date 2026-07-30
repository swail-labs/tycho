"""Per-harness eval assets, and the normalized projection the goldens are written in.

Not a test module — `test_harness_conformance.py` and the capture script both read this, so
"where does harness X's corpus live" has exactly one answer.

**Why a table instead of a directory move.** The captured transcripts predate this layout and
are referenced by ~16 test modules; relocating them would be a large diff that changes no
behavior. New assets (Stop payload, golden, capture metadata) live under
`fixtures/harness/<name>/`, and `TRANSCRIPTS` names the transcript wherever it already sits.
A capture written by `scripts/capture_harness.py` lands in the per-harness dir and the table
points at it — so the split closes on its own as real captures replace authored ones.
"""

from __future__ import annotations

import json
from pathlib import Path

from tycho.model import Event

FIXTURES = Path(__file__).parent / "fixtures"
HARNESS_DIR = FIXTURES / "harness"

# The transcript standing in for each harness's corpus. Claude's and Codex's were captured off
# the real binaries by `scripts/capture_harness.py` and live in the per-harness dir; Cursor's
# and OpenCode's came off real sessions before this layout existed and are named where they
# already sit. `capture.json` records which is which, and
# `test_an_enabled_harness_has_a_captured_corpus` is what holds an *enabled* harness to a
# tool-captured one.
TRANSCRIPTS = {
    "claude": HARNESS_DIR / "claude" / "transcript.jsonl",
    "cursor": HARNESS_DIR / "cursor" / "transcript.jsonl",
    "codex": HARNESS_DIR / "codex" / "transcript.jsonl",
    "opencode": FIXTURES / "opencode_transcript_sample.json",
}

# `capture.json` says a corpus was tool-captured; `TRANSCRIPTS` says which bytes the tests
# actually parse. Nothing tied the two together, so writing a capture flipped the eval's
# `corpus` column to "captured" while every test went on reading the authored sample — the
# scorecard sourcing its claim from a file no test consumes. Caught on Cursor's own capture.
def is_captured(name: str) -> bool:
    """Does `name`'s corpus claim hold up against the transcript the tests read?

    Both halves, deliberately: a `captured_by` stamp on a transcript the suite never opens is
    not a captured corpus, and a captured file nothing claims is not a re-verification.
    """
    path = TRANSCRIPTS[name]
    in_corpus = path.parent == HARNESS_DIR / name
    return bool(capture(name).get("captured_by")) and in_corpus


def payload(name: str) -> dict:
    """The captured Stop payload for ``name``, as the harness sends it."""
    return json.loads((HARNESS_DIR / name / "stop_payload.json").read_text(encoding="utf-8"))


def capture(name: str) -> dict:
    """Metadata about ``name``'s corpus: which harness version wrote it, and by what.

    Deliberately *not* the capability declaration — that lives on the `Harness` record in
    `tycho/read/harness.py`, because it is a property of the harness rather than of one
    capture, and a file next to the fixture would be a second source to drift. What genuinely
    belongs here is what only the capture knows: the version it came from, and whether a
    human authored it or a tool captured it.
    """
    path = HARNESS_DIR / name / "capture.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def golden_path(name: str) -> Path:
    return HARNESS_DIR / name / "golden.json"


def project(evts: tuple[Event, ...]) -> list[dict]:
    """Normalize Events into the shape goldens are pinned in.

    Not raw Events, on purpose. A golden over full Events breaks whenever a model writes a
    different command or a timestamp moves, which trains people to regenerate goldens without
    reading them — and a golden nobody reads catches nothing. This keeps the fields a reader
    bug actually shows up in: which tool, whether a command was recovered at all, whether a
    status was recorded, whether output survived, and the ordering.

    `has_*` rather than the values themselves for the same reason: that stdout *survived* is
    the reader contract; its exact bytes are the model's business.
    """
    return [
        {
            "tool": e.tool,
            "command": e.input.get("command"),
            "path": e.input.get("file_path") or e.input.get("filePath") or e.input.get("path"),
            "is_error": e.is_error,
            "has_result": bool(e.result),
            "has_output": bool(e.result.get("stdout") or e.result.get("stderr")),
            "has_ts": e.ts > 0,
        }
        for e in evts
    ]


def parsed(name: str) -> tuple[Event, ...]:
    """Run ``name``'s own reader over its corpus, through the registry rather than around it."""
    from tycho.read import harness as harness_mod

    return harness_mod.BY_NAME[name].parse(TRANSCRIPTS[name])

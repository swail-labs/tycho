"""Immutable data carriers + enums for the verifier. No I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Verdict(StrEnum):
    """The run-level answer Tycho renders."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"
    INDETERMINATE = "INDETERMINATE"
    OVERRIDDEN = "OVERRIDDEN"


class CheckStatus(StrEnum):
    """The per-check outcome. Reduces to a Verdict in verify.verdict_of."""

    PASS = "PASS"
    FAIL = "FAIL"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"
    INDETERMINATE = "INDETERMINATE"


class Stage(StrEnum):
    """The acceptance ladder (strategy §6.4): how far a turn got. Lowest to highest, a
    turn's stage being the highest rung it reached; `record.stage_of` computes it."""

    ATTEMPTED = "attempted"
    EXECUTED = "executed"
    ARTIFACT_CHANGED = "artifact_changed"
    CLAIM_SUPPORTED = "claim_supported"


@dataclass(frozen=True)
class Attribution:
    """Who produced a turn. Every field is None when the harness doesn't expose it, and is
    **never guessed** — the decay ledger slices catch rate by `model`."""

    model: str | None = None
    agent_version: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome plus the human-readable evidence for it."""

    name: str
    status: CheckStatus
    evidence: str


@dataclass(frozen=True)
class Event:
    """One normalized tool invocation from the harness transcript.

    `ts` is the completion time (result timestamp, falling back to invocation). `is_error`
    is the harness's failure signal; None means no result was captured. `result` holds the
    structured toolUseResult, or {} when absent or non-structured."""

    ts: float
    tool: str
    input: dict = field(default_factory=dict)
    is_error: bool | None = None
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """One assistant natural-language message, so `tool_call_provenance` can check the
    agent's *claims* against the tool `Event`s that actually happened."""

    ts: float
    text: str


@dataclass(frozen=True)
class CommandRun:
    """One command Tycho ran itself, via `tycho exec` — evidence the harness didn't produce.

    Unlike an `Event` (what a harness *chose* to write down, often without stdout or exit
    status), this is `wait()`'s status observed as the parent process, with nothing in
    between to mask it (strategy §9.6). `exit_code` is normalized to 128+signal."""

    cmd: str
    exit_code: int
    started_at: float
    ended_at: float

    @property
    def failed(self) -> bool:
        """True when the command Tycho ran returned non-zero. Never None, unlike a
        transcript's `is_error`."""
        return self.exit_code != 0


@dataclass(frozen=True)
class FileEdit:
    """A file the agent created or edited this session. `original` is the full content
    *before* the edit (None for a new file)."""

    path: str
    ts: float
    original: str | None
    kind: str  # "create" | "edit"


@dataclass(frozen=True)
class FileState:
    """Working-tree state of one file, read once in gather() so checks stay pure.
    `current_text` is the "after" side for the AST checks."""

    path: str
    exists: bool
    mtime: float | None
    current_text: str | None


@dataclass(frozen=True)
class GitSnapshot:
    """Repo state read once in gather()."""

    is_repo: bool
    head_sha: str | None
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class Session:
    """The gathered input snapshot the pure checks run against.

    Carries *both* scopes: "did this turn's edits land?" asks about `turn_edits`, while "is
    a source stale against the last green run?" asks about the whole session. So
    `turn_start` narrows a *view*; it never narrows `edits`/`events`."""

    events: tuple[Event, ...]
    edits: tuple[FileEdit, ...]
    repo: Path
    config: "Config"  # noqa: F821 — forward ref to tycho.config.Config, avoids import cycle
    files: Mapping[str, FileState] = field(default_factory=dict)
    git: GitSnapshot = field(default_factory=lambda: GitSnapshot(False, None, ()))
    has_tests: bool = True
    # Assistant prose, for tool_call_provenance. Empty for harnesses whose reader doesn't
    # supply it (the check then degrades to UNSUPPORTED, never a false verdict).
    messages: tuple[Message, ...] = ()
    # Empty for a harness that exposes none of it — the record stores nulls, not a guess.
    attribution: Attribution = Attribution()
    # Epoch the turn under review began; 0.0 means "the whole transcript is the turn".
    turn_start: float = 0.0
    # Commands Tycho ran itself. Already bounded to this session by gather's floor — the log
    # outlives every session, so all of it would let yesterday's green vouch for today.
    commands: tuple[CommandRun, ...] = ()

    @property
    def turn_edits(self) -> tuple[FileEdit, ...]:
        """The edits made by the turn under review (all of them when turn_start is 0.0)."""
        return tuple(fe for fe in self.edits if fe.ts >= self.turn_start)

    @property
    def turn_events(self) -> tuple[Event, ...]:
        """The events of the turn under review (all of them when turn_start is 0.0)."""
        return tuple(e for e in self.events if e.ts >= self.turn_start)

    @property
    def turn_messages(self) -> tuple[Message, ...]:
        """The assistant prose of the turn under review (all when turn_start is 0.0)."""
        return tuple(m for m in self.messages if m.ts >= self.turn_start)

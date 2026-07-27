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
    """The acceptance ladder (strategy §6.4): how far a turn actually got.

    `attempted -> executed -> artifact_changed -> claim_supported`, lowest to highest. A
    turn's stage is the highest rung it reached; `record.stage_of` computes it.

    It lives here with `Verdict`/`CheckStatus` rather than in `record.py` because it is a
    fact *about a turn*, not a storage detail: the digest, `blame`/`log` and the decay
    ledger all read it, and none of them should have to import the writer to name it.
    Values are lowercase — they're a progression, not a status; the visual distinction
    from a shouty `Verdict` is deliberate.
    """

    ATTEMPTED = "attempted"
    EXECUTED = "executed"
    ARTIFACT_CHANGED = "artifact_changed"
    CLAIM_SUPPORTED = "claim_supported"


@dataclass(frozen=True)
class Attribution:
    """Who produced a turn: the model, the agent build, and the session it belongs to.

    Every field is None when the harness doesn't expose it. **Never guessed** — the decay
    ledger slices catch rate by `model`, and a plausible-but-invented model id would make
    that measurement worse than not having it.
    """

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

    `ts` is the completion time (result timestamp, falling back to invocation).
    `is_error` is the harness's failure signal (Bash: non-zero exit or denied);
    None means no result was captured. `result` holds the structured
    toolUseResult (Bash: stdout/stderr; Edit/Write: filePath/originalFile/…),
    or {} when absent or non-structured.
    """

    ts: float
    tool: str
    input: dict = field(default_factory=dict)
    is_error: bool | None = None
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """One assistant natural-language message (the agent's prose, not a tool call).

    Carried so `tool_call_provenance` can check the agent's *claims* ("I created the
    ticket", "I searched the web") against the tool `Event`s that actually happened.
    Only assistant text is modeled — user prose and tool payloads live elsewhere.
    """

    ts: float
    text: str


@dataclass(frozen=True)
class CommandRun:
    """One command Tycho ran itself, via `tycho exec` — evidence the harness didn't produce.

    The distinction from `Event` is the whole point of strategy §9.6. An `Event` is what a
    *harness* chose to write down about a command, and three of the four harnesses choose
    to write down almost nothing: no stdout, no exit status, sometimes no result at all. A
    `CommandRun` is what Tycho observed by being the parent process — `wait()`'s status and
    the bytes off the pipe. Nothing sits between the runner and this, so nothing can mask,
    drop or head-truncate it.

    `exit_code` is already normalized to the shell's convention (128+signal), so `failed`
    is a plain comparison rather than a platform quiz. `output` is the **tail** of the run,
    redacted and bounded — see `command._MAX_CAPTURE_BYTES` for why the tail and not the head.
    """

    cmd: str
    exit_code: int
    started_at: float
    ended_at: float
    cwd: str = ""
    output: str = ""

    @property
    def failed(self) -> bool:
        """True when the command Tycho ran returned non-zero. Never None: unlike a
        transcript's `is_error`, this evidence either exists or the run isn't here."""
        return self.exit_code != 0


@dataclass(frozen=True)
class FileEdit:
    """A file the agent created or edited this session.

    `original` is the file's full content *before* the edit (None for a new
    file) — enough for the AST checks without touching git.
    """

    path: str
    ts: float
    original: str | None
    kind: str  # "create" | "edit"


@dataclass(frozen=True)
class FileState:
    """Working-tree state of one file, read once in gather() so checks stay pure.

    `current_text` is the on-disk content — the "after" side for the AST checks.
    """

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

    Carries *both* scopes, because the checks genuinely need both (TYCHO-17):
    "did this turn's edits land?" is a question about `turn_edits`, while "is a
    source stale against the last green run?" is a question about the whole
    session — a file edited three turns ago and never retested really is stale.
    So `turn_start` narrows a *view*; it never narrows `edits`/`events`.
    """

    events: tuple[Event, ...]
    edits: tuple[FileEdit, ...]
    repo: Path
    config: "Config"  # noqa: F821 — forward ref to tycho.config.Config, avoids import cycle
    files: Mapping[str, FileState] = field(default_factory=dict)
    git: GitSnapshot = field(default_factory=lambda: GitSnapshot(False, None, ()))
    has_tests: bool = True
    # Assistant prose, for tool_call_provenance. Empty for harnesses whose reader doesn't
    # supply it (the check then degrades to UNSUPPORTED there, never a false verdict).
    messages: tuple[Message, ...] = ()
    # Who produced this turn (model id, agent version, session id), read once in gather()
    # from the harness's `attribution` reader. Empty for a harness that exposes none of it
    # — the per-turn record then stores nulls rather than a guess.
    attribution: Attribution = Attribution()
    # Epoch at which the turn under review began. 0.0 means "the whole transcript is
    # the turn" — the honest default for `tycho verify` (a manual whole-session audit)
    # and for harnesses whose readers already hand us a single turn (Codex) or that
    # don't timestamp events at all (Cursor).
    turn_start: float = 0.0
    # Commands Tycho ran itself (`tycho exec`), read once in gather() from
    # `.tycho/commands.jsonl`. **Already bounded to this turn/session by gather's floor** —
    # the log outlives every session, so admitting all of it would let a green run from
    # yesterday vouch for a claim made today. Empty is the normal case: nobody has to use
    # `tycho exec`, and every check degrades to exactly its old behaviour when they don't.
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

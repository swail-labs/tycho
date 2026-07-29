"""Per-harness adapters for the Stop hook.

The engine runs on a normalized ``Session``. Three things differ between harnesses: where
the transcript lives, how the payload names the repo root (``cwd`` vs ``workspace_roots``),
and the JSON shape that surfaces a message to the human *without* blocking.
"""

from __future__ import annotations

import os
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import events
from . import opencode as opencode_mod
from ..store import state
from ..model import Attribution


@dataclass(frozen=True)
class Capabilities:
    """What a harness's transcript actually records — the honest declaration of its reach.

    Every field is required, deliberately: this is the one thing you cannot forget when
    adding a harness, because the constructor won't build without it. That makes "correctly
    blind" and "quietly broken" distinguishable in the suite — the conformance tests assert
    both directions, that a declared capability yields real data and an undeclared one
    degrades rather than guessing. Without the declaration the two look identical, and
    Tycho's characteristic failure is the quiet no-op that reads exactly like a clean turn.

    These describe the *transcript*, not the checks. ``records_exit_status`` says a status is
    written down, not that it is the status of anything useful — OpenCode records a piped
    pipeline's exit code, which is a real recorded status and still worthless for telling a
    red suite from a green one.
    """

    # A per-command exit status or error flag survives into the transcript.
    records_exit_status: bool
    # The runner's own stdout/stderr survives — what lets the engine re-read a suite's
    # summary when the shell masked the status. The highest-value field here.
    records_runner_output: bool
    # Events carry real timestamps, so a turn can be scoped to a boundary.
    records_timestamps: bool
    # Assistant prose is recoverable, for tool_call_provenance.
    records_prose: bool
    # Model id / agent version / session id are recorded, for the decay ledger.
    records_attribution: bool
    # An edit records the file's prior contents, so a diff needs no git baseline.
    records_edit_originals: bool
    # The harness labels its own turns, rather than the boundary being inferred.
    has_turn_ids: bool
    # The transcript is a file the harness maintains, not something Tycho rebuilds.
    transcript_is_file: bool


@dataclass(frozen=True)
class Channels:
    """How this harness carries Tycho's two audiences — declared, not inferred.

    Tycho always has at most two things to say about a turn: what a *person* needs to read,
    and what the *agent* is being asked to do. Every harness routes those differently, and the
    difference is not a detail — Codex shipped a whole release verifying correctly and telling
    nobody, because its human field is accepted and rendered nowhere, and the code that assumed
    otherwise was shaped like Claude's.

    So the shape is declared here and the assembly logic reads it, instead of branching on
    ``name``. Adding a harness means filling this in and writing one ``compose``; nothing in
    the engine or the hook needs to learn the new name.

    ``human_only`` is the valuable one: a field the human reads that the model does not is what
    makes a verdict free to show. Without it, reaching a person costs model tokens and a turn,
    which is why anything routed through ``shared`` is kept short.
    """

    # A field the human reads and the model does not. Claude's `systemMessage`, OpenCode's
    # toast. Where this is false, nothing Tycho says is free.
    human_only: bool
    # A field the model reads and the human does not. Claude's `additionalContext`.
    model_only: bool
    # One field both audiences read. Codex's `reason`, Cursor's `followup_message`. Reaching
    # the human this way costs a blocked Stop and a model turn, so it is used sparingly.
    shared: bool
    # The verdict relay is validated for this harness. A policy claim, not a shape: Cursor has
    # a shared channel and could carry a relay, but its loop behaviour has never been checked,
    # and a relay that cannot stop is worse than no relay.
    relays: bool


@dataclass(frozen=True)
class Harness:
    """How one harness reads its transcript, names the repo root, and formats output."""

    name: str
    parse: Callable[[Path], tuple]
    repo_root: Callable[[dict], Path]
    discover: Callable[[Path], Path | None]  # newest transcript for a repo, or None
    # Transcript to verify, from a hook payload. OpenCode rebuilds it from opencode.db into
    # a temp file the caller unlinks. None: nothing to verify.
    transcript_of: Callable[[dict], Path | None]
    # What this harness's transcript records, declared rather than inferred. Required, so a
    # new harness cannot enter the registry without saying what it can and cannot see.
    capabilities: Capabilities
    # Which audiences this harness can reach, and how. Required for the same reason.
    channels: Channels
    # Assemble the Stop output from the two texts the hook computed: ``human`` is what a person
    # must read, ``model`` the instruction for the agent. Either may be "". Returns the wire
    # dict, or None for silence. THE per-harness seam — the hook decides *what* is said and to
    # whom, this decides how it is spelled on the wire, and the two never mix.
    compose: Callable[[str, str], dict | None]
    # Every transcript on disk for a repo, oldest first — what `backfill` replays. Default
    # empty: a harness whose history Tycho can't enumerate backfills nothing rather than
    # guessing at one session.
    history: Callable[[Path], tuple[Path, ...]] = lambda _: ()
    # Epoch the turn under review began. 0.0 means "the whole transcript is the turn".
    turn_start: Callable[[Path], float] = lambda _: 0.0
    # Assistant prose, for tool_call_provenance. Default supplies none, so the check
    # degrades to UNSUPPORTED there rather than guessing.
    messages: Callable[[Path], tuple] = lambda _: ()
    # Model id, agent version, session id, for the per-turn record. Default supplies
    # nothing: an unattributable harness stores nulls, never a guess.
    attribution: Callable[[Path], Attribution] = lambda _: Attribution()
    # Human-only, and deliberately NOT `format_output`: on Cursor that field is model-facing,
    # and a notice reaching the model could commission a self-update. None suppresses it.
    notice_output: Callable[[str], dict] | None = None


def _payload_transcript(payload: dict) -> Path | None:
    """Claude/Cursor/Codex hand us the transcript path directly."""
    path = payload.get("transcript_path")
    return Path(path) if path else None


def _anchor(start: Path) -> Path:
    """The repo root the checks run against: the nearest ancestor Tycho is installed in.

    Not the harness's raw `cwd`. A `cd` inside a Bash call persists for the rest of the
    session, so an agent that ran `cd packages/slug && pytest` reports that subdirectory as
    its cwd at Stop. Anchoring there quietly re-bases everything: edits recorded as `slug.py`
    instead of `packages/slug/slug.py`, `[scope]` globs matched against the wrong root, and
    `git_state` comparing subdirectory-relative paths against git's repo-relative output —
    which reported "0 uncommitted" for a tree with two modified files. `state.root_for` is
    what `.tycho/` itself already resolves with, so this makes the two agree.
    """
    return state.root_for(start)


def _cwd_root(payload: dict) -> Path:
    return _anchor(Path(payload.get("cwd") or os.getcwd()))


def _cursor_root(payload: dict) -> Path:
    roots = payload.get("workspace_roots") or []
    return _anchor(Path(roots[0]) if roots else Path(os.getcwd()))


# Override order for a harness's data root: Tycho's env var, the harness's own, `~/.<name>`.
_NATIVE_HOME_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "cursor": None,  # no documented override
}


def home(name: str) -> Path:
    """Root of ``name``'s on-disk data — the dir holding ``projects/``/``sessions/``."""
    for var in (f"TYCHO_{name.upper()}_HOME", _NATIVE_HOME_ENV.get(name)):
        value = os.environ.get(var) if var else None
        if value:
            return Path(value).expanduser()
    return Path.home() / f".{name}"


# Transcripts are keyed by the start-time cwd with every separator turned into "-"; Claude
# keeps the leading dash, Cursor strips it. Checked against real dirs, Windows included.
_DIR_SEP_CHARS = str.maketrans({c: "-" for c in "\\/:. "})


def _encode(cwd: Path) -> str:
    return str(cwd).translate(_DIR_SEP_CHARS)


def _newest(paths) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def _claude_discover(cwd: Path) -> Path | None:
    root = home("claude") / "projects" / _encode(cwd)
    return _newest(root.glob("*.jsonl"))


def _claude_history(cwd: Path) -> tuple[Path, ...]:
    """Every Claude Code transcript this repo has, oldest first.

    The same directory `_claude_discover` takes the newest from — the history is already on
    disk from before Tycho was installed, which is the whole premise of `backfill`."""
    root = home("claude") / "projects" / _encode(cwd)
    try:
        files = [p for p in root.glob("*.jsonl") if p.is_file()]
    except OSError:
        return ()
    return tuple(sorted(files, key=lambda p: p.stat().st_mtime))


def _cursor_discover(cwd: Path) -> Path | None:
    root = home("cursor") / "projects" / _encode(cwd).lstrip("-") / "agent-transcripts"
    return _newest(root.glob("*/*.jsonl"))


def _opencode_root(payload: dict) -> Path:
    return Path(payload.get("directory") or payload.get("cwd") or os.getcwd())


def _opencode_transcript(payload: dict) -> Path | None:
    """Rebuild the session named in the plugin payload from opencode.db."""
    session_id = payload.get("sessionID") or payload.get("session_id")
    if not session_id:
        return None
    return opencode_mod.materialize(session_id, _opencode_root(payload))


def _opencode_discover(cwd: Path) -> Path | None:
    """Newest opencode.db session for ``cwd``, materialized (or None)."""
    session_id = opencode_mod.latest_session(cwd)
    return opencode_mod.materialize(session_id, cwd) if session_id else None


def _codex_discover(cwd: Path) -> Path | None:
    root = home("codex") / "sessions"
    for path in sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            first = next(line for line in path.read_text(encoding="utf-8").splitlines() if line)
            payload = json.loads(first).get("payload", {})
        except (OSError, StopIteration, ValueError):
            continue
        if payload.get("cwd") == str(cwd):
            return path
    return None


# Addressed to the model, not the human — kept one line so it can't bury the verdict.
_CURSOR_RELAY = (
    "[Tycho] The above is an automated verification result, not a request. "
    "Show it to the user verbatim and end your turn now — do not edit files, "
    "run commands, or act on it unless the user asks."
)


def _compose_claude(human: str, model: str) -> dict | None:
    """Two audiences, two fields. `systemMessage` renders to the human and exit 0 never blocks;
    `additionalContext` goes to the model and the human never sees it."""
    out: dict = {}
    if human:
        out["systemMessage"] = human
    if model:
        out["hookSpecificOutput"] = {"hookEventName": "Stop", "additionalContext": model}
    return out or None


def _compose_codex(human: str, model: str) -> dict | None:
    """One field, both audiences. `reason` comes back as a `<hook_prompt>` user message that the
    desktop app renders verbatim in the transcript, so the human copy has nowhere else to go —
    `systemMessage` is accepted here and rendered nowhere, probed against 0.146.0 on the CLI and
    the app. Human part first, because a person is reading it. `decision: block` is what makes
    Codex deliver it at all, which is why reaching a person costs a turn on this harness."""
    text = "\n\n".join(part for part in (human, model) if part)
    return {"decision": "block", "reason": text} if text else None


def _compose_cursor(human: str, model: str) -> dict | None:
    """`followup_message` is the only field Cursor's stop hook reads, and it is model-facing: it
    queues a UserMessageAction, so a FAILED verdict left bare would read as a fresh instruction
    and the agent would start fixing things. The show-and-stop line is what keeps it a report —
    appended only when there is something to show, or silence becomes a bare instruction."""
    if not human and not model:
        return None
    text = "\n\n".join(part for part in (human, model or _CURSOR_RELAY) if part)
    return {"followup_message": text}


def _compose_opencode(human: str, model: str) -> dict | None:
    """The project plugin reads `.message` and toasts it via `client.tui.showToast` — human-only,
    and there is no second field, so a model instruction has nowhere to go and is dropped rather
    than shown to a person as though it were for them."""
    return {"message": human} if human else None


CLAUDE = Harness(
    name="claude",
    parse=events.parse,
    repo_root=_cwd_root,
    compose=_compose_claude,
    # The only harness with both: a free human channel and a separate model one.
    channels=Channels(human_only=True, model_only=True, shared=False, relays=True),
    # Same human-only field; verified against 2.1.212 for SessionStart.
    notice_output=lambda text: {"systemMessage": text},
    discover=_claude_discover,
    transcript_of=_payload_transcript,
    # The most complete of the four: `is_error` per tool_result, stdout/stderr in
    # `toolUseResult`, ISO timestamps, text blocks, sessionId/version/model, and
    # `originalFile` on every edit. No turn ids — `turn_starts` infers boundaries.
    capabilities=Capabilities(
        records_exit_status=True,
        records_runner_output=True,
        records_timestamps=True,
        records_prose=True,
        records_attribution=True,
        records_edit_originals=True,
        has_turn_ids=False,
        transcript_is_file=True,
    ),
    history=_claude_history,
    turn_start=events.turn_start,
    messages=events.assistant_messages,
    attribution=events.attribution,
)

CURSOR = Harness(
    name="cursor",
    parse=events.parse_cursor,
    repo_root=_cursor_root,
    compose=_compose_cursor,
    # One model-facing field that the human also reads. `relays=False`: its loop_limit
    # behaviour under an injected verdict has never been checked, and an unstoppable relay is
    # worse than none.
    channels=Channels(human_only=False, model_only=False, shared=True, relays=False),
    discover=_cursor_discover,
    transcript_of=_payload_transcript,
    # The blindest of the four, and the reason this dataclass exists. Cursor's Stop
    # transcript carries only `name` + `input` per tool_use — no ids, no timestamps, no
    # tool_result at all. Everything false here is a structural fact about Cursor, printed
    # on the scorecard forever rather than mistaken for a Tycho bug.
    capabilities=Capabilities(
        records_exit_status=False,
        records_runner_output=False,
        records_timestamps=False,
        records_prose=False,
        records_attribution=False,
        records_edit_originals=False,
        has_turn_ids=False,
        transcript_is_file=True,
    ),
    # turn_start stays 0.0: parse_cursor gives every Event ts=0.0, so nothing to scope by.
)

CODEX = Harness(
    name="codex",
    parse=events.parse_codex,
    repo_root=_cwd_root,
    compose=_compose_codex,
    # No free channel at all: `systemMessage` is dropped, so everything Tycho says here costs a
    # blocked Stop and model tokens. Hence the short spellings on this path.
    channels=Channels(human_only=False, model_only=False, shared=True, relays=True),
    # None, like Cursor: Codex has no field that reaches a person without also reaching the
    # model, and a bootup notice there could tell an agent to go update Tycho. The Stop path
    # still speaks, via `compose` — it has a verdict worth a blocked turn. A "you're up to date"
    # toast is not.
    discover=_codex_discover,
    transcript_of=_payload_transcript,
    # Nearly Claude's reach, with one real gap: `patch_apply_end` names the changed paths
    # and whether it was an add or an update, but never the prior contents — so a Codex diff
    # needs git for its baseline where a Claude one does not. The only harness that labels
    # its own turns.
    capabilities=Capabilities(
        records_exit_status=True,
        records_runner_output=True,
        records_timestamps=True,
        records_prose=True,
        records_attribution=True,
        records_edit_originals=False,
        has_turn_ids=True,
        transcript_is_file=True,
    ),
    messages=events.assistant_messages_codex,
    # Anchors on the latest turn's task_started; Codex is the only harness with a turn_id.
    turn_start=events.turn_start_codex,
    attribution=events.attribution_codex,
)

OPENCODE = Harness(
    name="opencode",
    parse=events.parse_opencode,
    repo_root=_opencode_root,
    compose=_compose_opencode,
    # A free human channel (the toast) and nothing model-facing, so no relay is possible.
    channels=Channels(human_only=True, model_only=False, shared=False, relays=False),
    # The plugin reads `.message` and toasts it via client.tui.showToast — user-facing.
    notice_output=lambda text: {"message": text},
    # No transcript file — both paths rebuild it from opencode.db.
    discover=_opencode_discover,
    transcript_of=_opencode_transcript,
    # Records an exit status, and it is the *pipeline's* — true of `pytest | tee`, so it
    # says nothing about the runner. That is why `records_exit_status` is not the same
    # claim as "a lie has somewhere to show up": with no runner output kept, OpenCode
    # cannot tell a red suite from a green one behind a pipe. The only harness whose
    # transcript Tycho rebuilds rather than reads.
    capabilities=Capabilities(
        records_exit_status=True,
        records_runner_output=False,
        records_timestamps=True,
        records_prose=False,
        records_attribution=False,
        records_edit_originals=False,
        has_turn_ids=False,
        transcript_is_file=False,
    ),
    # Anchors on the last user message, which is what opens an OpenCode turn.
    turn_start=events.turn_start_opencode,
)

ALL = (CLAUDE, CURSOR, CODEX, OPENCODE)
BY_NAME = {h.name: h for h in ALL}

# Exposed in normal usage; the rest keep their adapters and unit tests but aren't wired.
# ponytail: single gate — re-widen by adding a name here.
#
# Codex joins Claude once its reader was checked against transcripts the current version
# actually wrote: it reads both shell-tool shapes, takes the exit status the rollout records,
# and reports model, version and session id. `VERIFIED_AGAINST` below is the standing claim.
ENABLED_NAMES = ("claude", "codex")
ENABLED = tuple(h for h in ALL if h.name in ENABLED_NAMES)

# The harness version each hook contract was last checked against, plus the local
# `--version` probe; a test pins the two together. OpenCode has no CLI version to probe.
#
# Bumping one of these is a claim that someone re-read the harness's output, not that the
# number looked old. What the claim covers, and how to repeat it:
#
#   1. `parse` finds tool events, and Bash events still carry `input.command`, an exit status
#      (`is_error`) and `toolUseResult` — the exit status is what `command_execution` reads,
#      and it going missing is the silent failure this pin exists to catch.
#   2. `turn_start`, `assistant_messages` and `attribution` return real values, the last with
#      model, agent version and session id.
#   3. `repo_root` and `transcript_of` still read `cwd` and `transcript_path`, `detect` still
#      routes the payload here, and `format_output`/`notice_output` still reach the human.
#
# Run those against a transcript the *new* version wrote, then capture rows from it into
# `tests/fixtures/transcript_attribution.jsonl`; a test holds that fixture to the pin, so the
# version here can only move when real data from that version moves with it.
VERIFIED_AGAINST = {
    "claude": {"version": "2.1.220", "probe": ("claude", "--version")},
    "cursor": {"version": "2026.07.09-a3815c0", "probe": ("cursor-agent", "--version")},
    # One pin covers both front ends: the CLI and the ChatGPT desktop app run the same core
    # against the same CODEX_HOME. Re-checked 2026-07-29 against a desktop-app turn
    # (`originator: "Codex Desktop"`, `cli_version: "0.146.0-alpha.3.1"`) and a CLI turn —
    # same rollout directory, same Stop payload, same event shapes.
    "codex": {"version": "0.146.0", "probe": ("codex", "--version")},
    # OpenCode ships no CLI version to probe, so drift here can only be caught by re-reading
    # a capture. The entry exists anyway: the version is recorded (its sessions carry
    # `info.version`, which is where this came from), and an explicit `probe: None` is a
    # decision on the record where a missing key reads as an oversight.
    "opencode": {"version": "1.17.20", "probe": None},
}


def detect(payload: dict) -> Harness:
    """Pick the harness from a hook payload's shape; default Claude.

    Not only Stop payloads. Codex's Stop carries a `turn_id` and nothing else here does, but its
    *SessionStart* carries `session_id`, `transcript_path`, `cwd`, `hook_event_name`,
    `permission_mode` and `source` — every one of which Claude's SessionStart also carries. So
    for a while a Codex bootup was read as Claude and answered on Claude's channel, which is a
    field Codex drops: the notice reached nobody and the code said it was "harmless, both emit
    `systemMessage`".

    The transcript's own location is what settles it, since each harness writes under its own
    home and `home()` already honours the overrides. Checked after the `turn_id` row so a Stop
    still resolves without touching the environment.
    """
    if payload.get("harness") == "opencode" or payload.get("sessionID"):
        return OPENCODE
    if payload.get("workspace_roots") is not None or payload.get("cursor_version"):
        return CURSOR
    if payload.get("hook_event_name") == "Stop" and payload.get("turn_id"):
        return CODEX
    if _transcript_under(payload, home("codex")):
        return CODEX
    return CLAUDE


def _transcript_under(payload: dict, root: Path) -> bool:
    """Does this payload's transcript live under ``root``? False on anything unreadable — a
    detection heuristic must never raise inside a hook."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    try:
        return Path(path).expanduser().is_relative_to(root.expanduser())
    except (OSError, ValueError):
        return False


def discover(cwd: Path, only: str | None = None) -> tuple[Path | None, Harness | None]:
    """Most-recently-used transcript for ``cwd``, as (path, harness) or (None, None).

    Cursor transcripts carry no internal timestamps, so file mtime is the only signal all
    harnesses share. ``only`` restricts discovery to one harness name."""
    candidates = []
    for harness in ENABLED:
        if only and harness.name != only:
            continue
        path = harness.discover(cwd)
        if path is not None:
            candidates.append((path.stat().st_mtime, path, harness))
    if not candidates:
        return None, None
    _, path, harness = max(candidates, key=lambda c: c[0])
    return path, harness

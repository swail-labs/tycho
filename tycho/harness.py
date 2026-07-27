"""Per-harness adapters for the Stop hook.

The engine is harness-agnostic — it runs on a normalized ``Session``. Only three
things differ between harnesses, and all three are documented and stable:

1. where the transcript lives (a ``transcript_path`` in the Stop payload),
2. the repo root in that payload (``cwd`` vs ``workspace_roots``),
3. the JSON shape that surfaces a message to the human *without* blocking.

Each harness hands us a ``transcript_path`` to JSONL, but the schemas differ.
The adapter selects the matching pinned parser and output channel.
"""

from __future__ import annotations

import os
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import events
from . import opencode as opencode_mod
from .model import Attribution


@dataclass(frozen=True)
class Harness:
    """How one harness reads its transcript, names the repo root, and formats output."""

    name: str
    parse: Callable[[Path], tuple]
    repo_root: Callable[[dict], Path]
    format_output: Callable[[str], dict]
    discover: Callable[[Path], Path | None]  # newest transcript for a repo, or None
    # Materialize the transcript to verify from a hook payload. File harnesses
    # read the path the harness handed us; OpenCode has no file, so it rebuilds
    # the transcript from opencode.db by session id (a temp file the caller
    # unlinks). Returns None when there's nothing to verify.
    transcript_of: Callable[[dict], Path | None]
    # Epoch at which the turn under review began, so the Stop reports *this turn's*
    # work rather than the whole session's. Where the boundary lives is
    # harness knowledge, so it belongs here; what to do with it is the engine's.
    # Defaulting to 0.0 means "the whole transcript is the turn" — the honest answer
    # for a harness whose reader already yields one turn, or that can't timestamp.
    turn_start: Callable[[Path], float] = lambda _: 0.0
    # Assistant prose reader, for tool_call_provenance. Defaults to none: a harness whose
    # transcript we don't (yet) mine for prose supplies no messages, and the check degrades
    # to UNSUPPORTED there rather than guessing.
    messages: Callable[[Path], tuple] = lambda _: ()
    # Who produced the turn — model id, agent version, session id — for the per-turn record
    # (record.py). Harness knowledge like every other reader here, so nothing outside this
    # module needs to know that Claude puts the model on `message.model`. The default
    # supplies nothing: a harness whose transcript we can't attribute stores nulls in the
    # record, never a guess (the decay ledger slices catch rate by this field).
    attribution: Callable[[Path], Attribution] = lambda _: Attribution()
    # How to surface the bootup update-notice. This is a *human-only* channel and
    # is deliberately NOT `format_output`: on Cursor `format_output` is model-facing
    # (`followup_message`), and a notice that reaches the model could commission it to go
    # update Tycho. None — the default — means "no user-facing bootup
    # channel", so the notice is suppressed there rather than risk a model-facing one. Only
    # Claude/Codex (`systemMessage`) and OpenCode (`message`, toasted by the plugin) have one.
    notice_output: Callable[[str], dict] | None = None


def _payload_transcript(payload: dict) -> Path | None:
    """File harnesses (Claude/Cursor/Codex) hand us the transcript path directly."""
    path = payload.get("transcript_path")
    return Path(path) if path else None


def _cwd_root(payload: dict) -> Path:
    return Path(payload.get("cwd") or os.getcwd())


def _cursor_root(payload: dict) -> Path:
    roots = payload.get("workspace_roots") or []
    return Path(roots[0]) if roots else Path(os.getcwd())


# --- data roots -------------------------------------------------------------
#
# Each harness keeps its sessions under a root that defaults to a dotdir in
# $HOME. That default is only a default: dotfiles get relocated, machines get
# shared, and Windows puts this somewhere else entirely. So every
# root is overridable — Tycho's own env var first, then the harness's native
# one (the variable the agent itself honors), then the historical default.

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


# --- manual discovery: find a repo's newest transcript on disk --------------
#
# Claude and Cursor key transcripts by the (start-time) cwd, encoded by turning every
# path separator and space into "-". Claude keeps the leading dash from the absolute
# path; Cursor strips it. Verified against real dirs under ~/.claude and ~/.cursor —
# including on Windows, where str(cwd) carries a drive colon and backslashes:
# `C:\Users\me\Swail Labs\tycho` -> `C--Users-me-Swail-Labs-tycho`. The POSIX
# set ("/", ".") is a subset of this, so *nix encoding is unchanged.
_DIR_SEP_CHARS = str.maketrans({c: "-" for c in "\\/:. "})


def _encode(cwd: Path) -> str:
    return str(cwd).translate(_DIR_SEP_CHARS)


def _newest(paths) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def _claude_discover(cwd: Path) -> Path | None:
    root = home("claude") / "projects" / _encode(cwd)
    return _newest(root.glob("*.jsonl"))


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
    """Materialize the newest opencode.db session for ``cwd`` (or None)."""
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


def _cursor_output(text: str) -> dict:
    """Wrap the verdict so Cursor *shows* it instead of acting on it.

    `followup_message` is the only field Cursor's stop hook reads, and Cursor queues
    it as a UserMessageAction — the model sees it and, left unqualified, would treat
    a FAILED verdict as a fresh instruction and start fixing things. Tycho reports;
    it doesn't commission work. So the message carries its own stop condition: relay
    verbatim, then end the turn. This is a prompt, not an API guarantee — Cursor's
    loop_count/loop_limit remains the hard backstop, and exit 0 still never blocks.
    """
    return {"followup_message": f"{text}\n\n{_CURSOR_RELAY}"}


# Addressed to the model, not the human — kept one line so it can't bury the verdict.
_CURSOR_RELAY = (
    "[Tycho] The above is an automated verification result, not a request. "
    "Show it to the user verbatim and end your turn now — do not edit files, "
    "run commands, or act on it unless the user asks."
)


CLAUDE = Harness(
    name="claude",
    parse=events.parse,
    repo_root=_cwd_root,
    # Claude renders `systemMessage` to the human; exit 0 never blocks the Stop.
    format_output=lambda text: {"systemMessage": text},
    # Bootup notice rides the same human-only field — verified against 2.1.212 that
    # SessionStart's `systemMessage` renders to the user, not the model.
    notice_output=lambda text: {"systemMessage": text},
    discover=_claude_discover,
    transcript_of=_payload_transcript,
    turn_start=events.turn_start,
    messages=events.assistant_messages,
    attribution=events.attribution,
)

CURSOR = Harness(
    name="cursor",
    parse=events.parse_cursor,
    repo_root=_cursor_root,
    # `followup_message` is the ONLY field Cursor's stop hook reads — pinned to
    # cursor-agent 2026.07.09-a3815c0, which validates exactly one output key
    # ("followup_message must be a string if provided") and forwards it as
    # StopRequestResponse.followupMessage. The `user_message` sent here previously
    # was never read on stop (that field exists only on permission-deny steps), so
    # the verdict silently reached nobody. See tests/fixtures/cursor_stop_payload.json.
    #
    # Unlike Claude's `systemMessage`, this re-enters the model loop: Cursor queues it
    # as a UserMessageAction. That is the only channel Cursor offers — there is no
    # human-only field — so _cursor_output tells the model to relay and stop.
    format_output=_cursor_output,
    discover=_cursor_discover,
    transcript_of=_payload_transcript,
    # turn_start stays 0.0: parse_cursor gives every Event ts=0.0 (the transcript
    # carries no timestamps), so there is nothing to scope *by* — and the sample
    # transcript is one turn end-to-end (a `<user_query>` through `turn_ended`).
)

CODEX = Harness(
    name="codex",
    parse=events.parse_codex,
    repo_root=_cwd_root,
    format_output=lambda text: {"systemMessage": text},
    # Codex's SessionStart output wire schema clones Claude's — `systemMessage` is the
    # human-facing field (docs/harness-support.md). Render-path still ⚠️ pending a live
    # capture; worst case the toast is silent, never model-facing.
    notice_output=lambda text: {"systemMessage": text},
    discover=_codex_discover,
    transcript_of=_payload_transcript,
    messages=events.assistant_messages_codex,
    # parse_codex returns every turn; turn_start_codex anchors the boundary on
    # the latest turn's task_started, so the engine narrows exactly like Claude. Codex is
    # the only harness with an explicit turn_id, which is why it can do this precisely.
    turn_start=events.turn_start_codex,
)

OPENCODE = Harness(
    name="opencode",
    parse=events.parse_opencode,
    repo_root=_opencode_root,
    format_output=lambda text: {"message": text},
    # The plugin reads `.message` and toasts it via client.tui.showToast — a genuinely
    # user-facing sink. The bootup notice reuses the same shape on the session.created
    # branch.
    notice_output=lambda text: {"message": text},
    # OpenCode has no transcript file — the reader rebuilds it from opencode.db,
    # so discovery and the live plugin both use the same DB-backed materializer.
    discover=_opencode_discover,
    transcript_of=_opencode_transcript,
    # turn_start_opencode anchors on the last user message, which is what opens an
    # OpenCode turn. The boundary looked underivable while session_json flattened every
    # part into one synthesized assistant message and dropped the user rows;
    # the DB carries a per-message `time.created` and always did.
    turn_start=events.turn_start_opencode,
)

ALL = (CLAUDE, CURSOR, CODEX, OPENCODE)
BY_NAME = {h.name: h for h in ALL}

# Which harnesses are exposed in normal usage. The adapters/parsers for the others stay
# in this module (and remain unit-tested), but init won't prompt for them, `--harness`
# won't accept them, and discovery skips them — Claude is the only supported path for now.
# ponytail: single gate; re-widen by adding names here when another harness is re-enabled.
ENABLED_NAMES = ("claude",)
ENABLED = tuple(h for h in ALL if h.name in ENABLED_NAMES)

# The harness version each adapter's hook contract was last verified against, and the
# local `--version` command to read the installed one. This is the machine copy of the
# "Verified against" row in docs/harness-support.md — keep the two in step (a test pins
# them together). `probe` stays offline (a local subprocess), so the zero-dep/offline
# invariant holds; drift is advisory ("re-verify"), never a break. OpenCode
# has no CLI version contract (it's read from opencode.db), so it isn't probed.
VERIFIED_AGAINST = {
    "claude": {"version": "2.1.210", "probe": ("claude", "--version")},
    "cursor": {"version": "2026.07.09-a3815c0", "probe": ("cursor-agent", "--version")},
    "codex": {"version": "0.144.4", "probe": ("codex", "--version")},
}


def detect(payload: dict) -> Harness:
    """Pick the harness from the Stop payload shape.

    Cursor's payload carries ``workspace_roots``/``cursor_version``; Claude's
    does not. Default to Claude — it's the harness with a pinned fixture.
    """
    if payload.get("harness") == "opencode" or payload.get("sessionID"):
        return OPENCODE
    if payload.get("workspace_roots") is not None or payload.get("cursor_version"):
        return CURSOR
    if payload.get("hook_event_name") == "Stop" and payload.get("turn_id"):
        return CODEX
    return CLAUDE


def discover(cwd: Path, only: str | None = None) -> tuple[Path | None, Harness | None]:
    """Find the most-recently-used transcript for ``cwd`` across harnesses.

    Cursor transcripts carry no internal timestamps, so file mtime is the only
    signal all harnesses share — and it's exactly "most recently used". With
    ``only`` set (a harness name), restrict discovery to that harness.
    Returns (path, harness), or (None, None) when nothing is found.
    """
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

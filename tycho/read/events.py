"""Parse a Claude Code transcript (JSONL) into normalized Events + FileEdits.

Transcript schema (verified against a real session): each line is a JSON entry with a
top-level `timestamp` and a `message.content` list of blocks; an assistant `tool_use` block
carries `id`, `name`, `input`; a user `tool_result` block carries `tool_use_id`, `is_error`,
and the entry carries the structured `toolUseResult` (Bash: stdout/stderr; Edit/Write:
filePath/originalFile/type/…). Bash results have no exit code — `is_error` is the signal.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..engine import runlog
from ..model import UNSTRUCTURED_RESULT, Attribution, Event, FileEdit, Message

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})


def _entries(transcript: Path):
    """Yield parsed JSONL entries, skipping blank/malformed lines — this is external data.

    Streamed, and `errors="replace"`: a transcript is other people's bytes. Reading it whole
    held the file and a list of every line at once (~4.7x its size in RSS, and `gather` reads
    it four times), and one invalid byte raised out of every reader — which the hook swallows,
    so Tycho would go quiet for that session forever while the bad byte sat there."""
    with Path(transcript).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse(transcript: Path) -> tuple[Event, ...]:
    """Read a Claude Code JSONL transcript → Events sorted by completion time."""
    uses: dict[str, tuple[float, str, dict]] = {}
    results: dict[str, tuple[float, bool, dict]] = {}

    for entry in _entries(transcript):
        ts = _epoch(entry.get("timestamp"))
        for block in _blocks(entry):
            kind = block.get("type")
            if kind == "tool_use" and block.get("id"):
                uses[block["id"]] = (ts, block.get("name", ""), block.get("input") or {})
            elif kind == "tool_result" and block.get("tool_use_id"):
                tur = entry.get("toolUseResult")
                # A command that reached the shell comes back structured (stdout/stderr). One
                # the harness *refused* — unapproved permission, denied tool — comes back as a
                # bare string ("Error: This command requires approval") with `is_error` set.
                # Both used to flatten to `{}`, so a refusal was indistinguishable from a red
                # suite and `command_execution` reported FAIL for a command that never ran.
                # `UNSTRUCTURED_RESULT` keeps that difference readable downstream.
                results[block["tool_use_id"]] = (
                    ts,
                    bool(block.get("is_error")),
                    tur if isinstance(tur, dict) else {UNSTRUCTURED_RESULT: str(tur or "")},
                )

    events = []
    for uid, (use_ts, name, inp) in uses.items():
        result = results.get(uid)
        if result is None:
            events.append(Event(ts=use_ts, tool=name, input=inp, is_error=None, result={}))
        else:
            res_ts, is_error, res = result
            events.append(
                Event(ts=res_ts or use_ts, tool=name, input=inp, is_error=is_error, result=res)
            )
    return tuple(sorted(events, key=lambda e: e.ts))


def assistant_messages(transcript: Path) -> tuple[Message, ...]:
    """The assistant's natural-language `text` blocks — its prose claims, for
    `tool_call_provenance`. Skips tool_use/tool_result blocks and harness meta."""
    out = []
    for entry in _entries(transcript):
        if entry.get("type") != "assistant" or entry.get("isMeta"):
            continue
        ts = _epoch(entry.get("timestamp"))
        for block in _blocks(entry):
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    out.append(Message(ts=ts, text=text))
    return tuple(out)


def attribution(transcript: Path) -> Attribution:
    """Who produced this Claude Code session: model id, harness version, session id.

    Verified against real transcripts under ``~/.claude/projects`` (2026-07): every row
    carries top-level ``sessionId`` and ``version``, every ``type: "assistant"`` row carries
    ``message.model``. The *last* of each wins — a session can be resumed under a newer build
    or a different model. Never guesses: a field this build doesn't write yields None, since
    the decay ledger is only worth anything if attribution is genuinely observed.
    """
    model = version = session_id = None
    try:
        for entry in _entries(transcript):
            if isinstance(sid := entry.get("sessionId"), str) and sid:
                session_id = sid
            if isinstance(ver := entry.get("version"), str) and ver:
                version = ver
            if entry.get("type") == "assistant":
                candidate = (entry.get("message") or {}).get("model")
                if isinstance(candidate, str) and candidate:
                    model = candidate
    except OSError:
        return Attribution()
    return Attribution(model=model, agent_version=version, session_id=session_id)


def turn_starts(transcript: Path) -> tuple[float, ...]:
    """Every turn boundary in the transcript, ascending — a user message or a verdict-relay
    boundary opens a turn.

    Anchored on the *user* side, never on `end_turn` markers: one turn routinely emits two
    adjacent `end_turn` messages, so counting markers over-counts turns and puts the boundary
    inside the turn it should open (15 markers vs 11 user messages on the verified session).
    A content *list* is how Claude returns `tool_result` blocks to itself, so it only counts
    when it carries a genuine `text` block.

    Relay iterations share one user turn, so without the `stop_hook_summary` boundary each
    re-check would inherit earlier iterations' prose and re-fail claims already answered.

    The Stop hook wants only the last of these (`turn_start`); `backfill` wants all of them,
    to cut a whole transcript into the turns that produced it. One definition, so a replayed
    turn is bounded exactly as the live one was.
    """
    starts = sorted(
        _epoch(e.get("timestamp"))
        for e in _entries(transcript)
        if _is_user_prose(e) or _is_relay_boundary(e)
    )
    return tuple(ts for ts in starts if ts)


def turn_start(transcript: Path) -> float:
    """Epoch at which the turn under review began — the last boundary `turn_starts` found.
    0.0 when there is none ("the whole transcript is the turn").

    This Stop's own relay summary is written after this runs, so the last boundary is the
    previous one.
    """
    starts = turn_starts(transcript)
    return starts[-1] if starts else 0.0


# The fixed sentence in a relay `stop_hook_summary`'s injected context: stable across the
# "re-check N of M" / "final re-check" variants, and unique to the relay.
_RELAY_BOUNDARY = "automated verification of the turn you just finished"


def _is_relay_boundary(entry: dict) -> bool:
    """True for a Tycho verdict-relay Stop summary — the boundary opening the next relay
    iteration's turn."""
    if entry.get("type") != "system" or entry.get("subtype") != "stop_hook_summary":
        return False
    ctx = entry.get("hookAdditionalContext")
    if isinstance(ctx, (list, tuple)):
        ctx = " ".join(str(x) for x in ctx)
    return isinstance(ctx, str) and _RELAY_BOUNDARY in ctx


def _is_user_prose(entry: dict) -> bool:
    """True for a real user message — not a tool_result carrier, not harness meta."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return any(b.get("type") == "text" for b in _blocks(entry))


def parse_cursor(transcript: Path) -> tuple[Event, ...]:
    """Read a Cursor agent transcript → Events.

    Cursor's Stop transcript carries only ``name`` + ``input`` per ``tool_use`` — no ids, no
    timestamps, no ``tool_result`` blocks — so Events get ts=0.0, is_error=None, result={},
    and checks needing timings or exit codes degrade to UNSUPPORTED rather than guessing.
    """
    events = []
    for entry in _entries(transcript):
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("name"):
                tool = {"Shell": "Bash", "StrReplace": "Edit"}.get(block["name"], block["name"])
                events.append(Event(ts=0.0, tool=tool, input=block.get("input") or {}))
    return tuple(events)


# Codex spells one shell run two ways, and both are live in the same release: the freeform
# `exec` tool arrives as `custom_tool_call` carrying a JS snippet, the structured
# `exec_command` tool as `function_call` carrying JSON. Reading only the first made Tycho
# blind to whole sessions — no events, so the Stop saw no verifiable activity and stayed
# silent, which is indistinguishable from a clean turn.
_CODEX_CALL_TYPES = ("custom_tool_call", "function_call")
_CODEX_OUTPUT_TYPES = ("custom_tool_call_output", "function_call_output")


def parse_codex(transcript: Path) -> tuple[Event, ...]:
    """Read *every* turn from a Codex rollout JSONL transcript, not just the latest, so
    session-scoped checks can reason across turns; the Stop narrows via ``turn_start_codex``,
    exactly as Claude does. Filtering to the latest ``turn_id`` here would blind those checks
    and make Codex's scope disagree with every other reader."""
    calls: dict[str, tuple[float, str]] = {}
    results: dict[str, tuple[bool | None, str]] = {}  # call_id -> (is_error, output text)
    events = []
    for entry in _entries(transcript):
        payload = entry.get("payload") or {}
        ts = _epoch(entry.get("timestamp"))
        if entry.get("type") == "response_item" and payload.get("type") in _CODEX_CALL_TYPES:
            command = _codex_command_of(payload)
            if command:
                calls[payload.get("call_id", "")] = (ts, command)
        elif entry.get("type") == "response_item" and payload.get("type") in _CODEX_OUTPUT_TYPES:
            output = payload.get("output")
            results[payload.get("call_id", "")] = (_codex_is_error(output), _codex_output_text(output))
        elif entry.get("type") == "event_msg" and payload.get("type") == "patch_apply_end":
            if not payload.get("success"):
                continue
            for path, change in (payload.get("changes") or {}).items():
                kind = change.get("type", "update")
                events.append(
                    Event(
                        ts=ts,
                        tool="Write",
                        input={"path": path},
                        is_error=False,
                        result={"type": "create" if kind == "add" else "edit"},
                    )
                )
    events.extend(
        Event(
            ts=ts,
            tool="Bash",
            input={"command": command},
            is_error=results.get(call_id, (None, ""))[0],
            # Keep the runner's own words: `is_error` is useless once the shell masks the
            # status, and the engine can only re-read output the reader kept.
            result={"stdout": out} if (out := results.get(call_id, (None, ""))[1]) else {},
        )
        for call_id, (ts, command) in calls.items()
    )
    return tuple(sorted(events, key=lambda e: e.ts))


def assistant_messages_codex(transcript: Path) -> tuple[Message, ...]:
    """Codex assistant prose. Codex mirrors these as ``event_msg.agent_message`` too, so only
    the response item is read — otherwise every claim counts twice."""
    messages = []
    for entry in _entries(transcript):
        payload = entry.get("payload") or {}
        if (
            entry.get("type") != "response_item"
            or payload.get("type") != "message"
            or payload.get("role") != "assistant"
        ):
            continue
        ts = _epoch(entry.get("timestamp"))
        for block in payload.get("content") or []:
            text = block.get("text") if block.get("type") == "output_text" else None
            if isinstance(text, str) and text.strip():
                messages.append(Message(ts=ts, text=text))
    return tuple(messages)


def attribution_codex(transcript: Path) -> Attribution:
    """Who produced this Codex session: model id, CLI version, session id.

    Verified against real rollouts under ``~/.codex/sessions`` (2026-07): ``session_meta``
    opens every file with ``session_id`` and ``cli_version``, and each ``turn_context`` names
    the ``model`` (e.g. ``gpt-5.6-sol``). The *last* of each wins, as in the Claude reader — a
    resumed session can continue under a newer build or a different model, and the turn being
    verified ran under the latest. Never guesses: a field this build doesn't write yields
    None, since the decay ledger is only worth anything if attribution was observed.
    """
    model = version = session_id = None
    try:
        for entry in _entries(transcript):
            payload = entry.get("payload") or {}
            if entry.get("type") == "session_meta":
                if isinstance(sid := payload.get("session_id"), str) and sid:
                    session_id = sid
                if isinstance(ver := payload.get("cli_version"), str) and ver:
                    version = ver
            elif entry.get("type") == "turn_context":
                if isinstance(name := payload.get("model"), str) and name:
                    model = name
    except OSError:
        return Attribution()
    return Attribution(model=model, agent_version=version, session_id=session_id)


def turn_start_codex(transcript: Path) -> float:
    """Epoch of the latest Codex turn's ``task_started`` — the boundary its Stop reviews.

    Anchors on the latest turn that actually has tool calls or edits: Codex emits empty
    trailing turns (compaction follow-ups), and anchoring on one would push the boundary past
    all real work and blank out ``turn_edits``. 0.0 when there is no such turn."""
    entries = list(_entries(transcript))
    turn_ids = [
        e.get("payload", {}).get("turn_id")
        for e in entries
        if e.get("type") == "event_msg" and e.get("payload", {}).get("type") == "task_started"
    ]
    turn_id = _codex_latest_turn_with_events(entries, turn_ids)
    starts = [_codex_relay_boundary(e) for e in entries]
    relay_start = max((ts for ts in starts if ts), default=0.0)
    if turn_id is None:
        return relay_start
    for e in entries:
        p = e.get("payload") or {}
        if e.get("type") == "event_msg" and p.get("type") == "task_started" and p.get("turn_id") == turn_id:
            return max(_epoch(e.get("timestamp")), relay_start)
    return relay_start


def _codex_relay_boundary(entry: dict) -> float:
    """Timestamp of a Tycho continuation prompt, or 0. Codex keeps relay iterations inside one
    ``turn_id``, writing the rejected Stop back as a user ``<hook_prompt>`` item, so that
    injection — not ``task_started`` — opens the next iteration."""
    payload = entry.get("payload") or {}
    if (
        entry.get("type") != "response_item"
        or payload.get("type") != "message"
        or payload.get("role") != "user"
    ):
        return 0.0
    text = "\n".join(
        block.get("text", "")
        for block in payload.get("content") or []
        if block.get("type") == "input_text"
    )
    return _epoch(entry.get("timestamp")) if (
        text.startswith("<hook_prompt ") and _RELAY_BOUNDARY in text
    ) else 0.0


def _opencode_data(transcript: Path) -> dict:
    """Load an OpenCode session JSON (``{info, messages:[{info, parts:[…]}]}``)."""
    text = Path(transcript).read_text(encoding="utf-8", errors="replace")
    return json.loads(text[text.index("{"):])


def parse_opencode(transcript: Path) -> tuple[Event, ...]:
    """Read an OpenCode session JSON (``{info, messages:[{parts:[…]}]}``). Tycho rebuilds this
    shape from ``opencode.db`` (see ``opencode.py``); it matches what ``opencode export``
    emits."""
    data = _opencode_data(transcript)
    events = []
    for message in data.get("messages", []):
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            timing = state.get("time") or {}
            metadata = state.get("metadata") or {}
            status = state.get("status")
            exit_code = metadata.get("exit")
            is_error = (
                exit_code != 0 if isinstance(exit_code, int)
                else True if status == "error"
                else None
            )
            tool = {"bash": "Bash", "edit": "Edit", "write": "Write"}.get(
                part.get("tool"), part.get("tool", "")
            )
            events.append(Event(
                ts=(timing.get("end") or timing.get("start") or 0) / 1000,
                tool=tool,
                input=state.get("input") or {},
                is_error=is_error,
                result={"type": "edit"} if tool in _EDIT_TOOLS else {},
            ))
    return tuple(sorted(events, key=lambda e: e.ts))


def turn_start_opencode(transcript: Path) -> float:
    """Epoch of the OpenCode session's last user message — the boundary its Stop reviews, 0.0
    when none carries a timestamp.

    OpenCode timestamps in ms; ``parse_opencode`` gives ``Event.ts`` in seconds, so the
    boundary is divided to match. A ms boundary against second-scale events would sit ~1000x
    in the future and silently blank out ``turn_edits``.
    """
    starts = [
        created
        for message in _opencode_data(transcript).get("messages", [])
        if (info := message.get("info") or {}).get("role") == "user"
        and isinstance(created := (info.get("time") or {}).get("created"), (int, float))
    ]
    return max(starts, default=0) / 1000


def _codex_latest_turn_with_events(entries: list[dict], turn_ids: list[str]) -> str | None:
    """Find the most recent turn that actually has tool calls or edits."""
    for tid in reversed(turn_ids):
        for e in entries:
            p = e.get("payload") or {}
            tagged = p.get("turn_id") or (p.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id")
            if tagged != tid:
                continue
            if e.get("type") == "response_item" and p.get("type") in _CODEX_CALL_TYPES:
                return tid
            if e.get("type") == "event_msg" and p.get("type") == "patch_apply_end":
                return tid
    return None


def _codex_command_of(payload: dict) -> str | None:
    """The shell command one Codex tool call ran, or None if it ran none.

    Two encodings for the same act: `custom_tool_call` passes a JS snippet in ``input``,
    `function_call` passes JSON in ``arguments``. Session plumbing — ``wait``, ``write_stdin``
    — comes through as a `function_call` with no ``cmd``; those drive an already-running
    shell, and counting them would invent runs that never happened.
    """
    if payload.get("type") == "custom_tool_call":
        # Whitelist, not blacklist: `input` is free text on every other tool — `apply_patch`
        # carries patch bodies — and a substring hit there would fabricate a run that never
        # happened. Going blind to a renamed tool is the safer failure, and the version pin
        # is what catches it.
        if payload.get("name") != "exec":
            return None
        return _codex_command(payload.get("input"))
    try:
        arguments = json.loads(payload.get("arguments") or "{}")
    except (TypeError, ValueError):
        return None
    command = arguments.get("cmd") if isinstance(arguments, dict) else None
    return command if isinstance(command, str) and command.strip() else None


# `cmd` in either spelling Codex emits — `{cmd:"…"}` and `{"cmd":"…"}` both appear in the
# same release — with the value matched escape-aware so an embedded `\"` can't end it early.
_CODEX_CMD = re.compile(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')


def _codex_command(value: object) -> str | None:
    """The command out of an `exec` call's JS snippet.

    Scanning for the one literal `{cmd:"` and then guessing which of four delimiters closed
    the value dropped every call using the quoted spelling — 49 of the exec calls in a single
    real session — and truncated any command containing an escaped quote at that quote, so a
    `python3 -c "…"` run reached the checks as a fragment of itself. One escape-aware match
    covers both spellings and stops guessing.
    """
    if not isinstance(value, str):
        return None
    match = _CODEX_CMD.search(value)
    if not match:
        return None
    raw = match.group(1)
    try:
        command = json.loads(f'"{raw}"')
    except ValueError:
        # A JS-only escape JSON won't take (`\'`). Keep the text rather than drop the run.
        command = raw.replace('\\"', '"')
    return command if command.strip() else None


def _codex_output_text(value: object) -> str:
    """Flatten a Codex tool-call output into the text the command printed — Codex nests it
    (`[{"type": "input_text", "text": "77 passed in 0.79s"}]`). Normalizing it into `result`
    lets the harness-agnostic engine re-read a runner's verdict when the shell masked the
    exit status."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(text := value.get("text"), str):
            return text
        return "\n".join(t for v in value.values() if (t := _codex_output_text(v)))
    if isinstance(value, list):
        return "\n".join(t for item in value if (t := _codex_output_text(item)))
    return ""


# A real exit status, in the two places a Codex rollout records one. `Process exited with
# code N` is Codex's own framing on a structured tool result — line-anchored and taken
# first-match, because the header precedes the `Output:` block and a command that prints the
# same phrase (grepping a log, echoing a transcript) must not overwrite the recorded status.
_CODEX_EXIT_PATTERNS = (
    re.compile(r'"exit_code"\s*:\s*(\d+)'),
    re.compile(r"^Process exited with code (\d+)$", re.MULTILINE),
)


def _codex_is_error(value: object) -> bool | None:
    if isinstance(value, dict):
        if isinstance(value.get("exit_code"), int):
            return value["exit_code"] != 0
        return next((r for item in value.values() if (r := _codex_is_error(item)) is not None), None)
    if isinstance(value, list):
        return next((r for item in value if (r := _codex_is_error(item)) is not None), None)
    if isinstance(value, str):
        for pattern in _CODEX_EXIT_PATTERNS:
            if match := pattern.search(value):
                return int(match.group(1)) != 0
        # The freeform shape records no status at all, so fall back to the runner's own
        # summary. ("Script completed" is not success — it appears for failed commands too.)
        return runlog.outcome(value)
    return None


def file_edits(events: tuple[Event, ...]) -> tuple[FileEdit, ...]:
    """Project the *successful* Edit/Write events into FileEdits (path, ts, before-content).

    A failed call never touched the disk and, having no ``toolUseResult``, would land here as
    a phantom ``kind=create, original=None`` row. ``is_error is None`` means *no status was
    recorded*, not failure, so those still count — Cursor never records one."""
    out = []
    for e in events:
        if e.tool not in _EDIT_TOOLS or e.is_error:
            continue
        path = (
            e.input.get("file_path")
            or e.input.get("filePath")
            or e.input.get("path")
            or e.result.get("filePath")
        )
        if not path:
            continue
        original = e.result.get("originalFile")
        kind = e.result.get("type") or ("edit" if original is not None else "create")
        out.append(FileEdit(path=path, ts=e.ts, original=original, kind=kind))
    return tuple(out)


def _blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _epoch(stamp: object) -> float:
    # A harness may emit an epoch number rather than an ISO string; anything else is unknown.
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return float(stamp)
    if not isinstance(stamp, str) or not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0

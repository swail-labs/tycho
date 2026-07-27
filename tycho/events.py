"""Parse a Claude Code transcript (JSONL) into normalized Events + FileEdits.

Transcript schema (verified against a real session):
- each line is a JSON entry with a top-level `timestamp` and a `message.content`
  list of blocks;
- an assistant `tool_use` block carries `id`, `name`, `input`;
- a user `tool_result` block carries `tool_use_id`, `is_error`, and the entry
  carries the structured `toolUseResult` (Bash: stdout/stderr; Edit/Write:
  filePath/originalFile/type/…).
Bash results have no numeric exit code — `is_error` is the failure signal.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import runlog
from .model import Attribution, Event, FileEdit, Message

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})


def _entries(transcript: Path):
    """Yield parsed JSONL entries, skipping blank/malformed lines (external data)."""
    for line in Path(transcript).read_text(encoding="utf-8").splitlines():
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
                results[block["tool_use_id"]] = (
                    ts,
                    bool(block.get("is_error")),
                    tur if isinstance(tur, dict) else {},
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
    """The assistant's natural-language `text` blocks — the agent's prose claims, for
    `tool_call_provenance`. Skips tool_use/tool_result blocks and harness meta; a message
    with no real text is dropped. Same JSONL schema as `parse` (verified fixture)."""
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
    carries a top-level ``sessionId`` and the harness ``version`` (e.g. ``"2.1.220"``), and
    every ``type: "assistant"`` row carries ``message.model`` (e.g. ``"claude-opus-5"``).
    The *last* value of each wins — a session can be resumed under a newer harness build or
    a different model, and the turn a Stop is recording is the most recent one.

    Never raises and never guesses: an unreadable transcript, or a field this harness build
    doesn't write, yields None. The record stores that null rather than a plausible value —
    the decay ledger is only worth anything if its model attribution is genuinely observed.
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


def turn_start(transcript: Path) -> float:
    """Epoch at which the turn under review began — the later of the final user message
    and the last verdict-relay boundary.

    A Claude turn is `user prose -> assistant work -> assistant stop_reason=end_turn`,
    and the Stop hook fires at the end of the last one. We anchor on the *user* side
    rather than counting `end_turn` markers: on real sessions one turn routinely emits
    two adjacent `end_turn` assistant messages (a contentful one preceded by an empty
    one), so a marker count over-counts turns and would place the boundary inside the
    turn it's meant to open. User messages have no such duplication — on the session
    this was verified against, 15 `end_turn` markers coalesce to exactly the 11 user
    messages, alternating cleanly.

    Real user prose arrives as a plain `message.content` string; a content *list* is
    how Claude delivers `tool_result` blocks back to itself, so a list only counts as
    a turn start when it carries a genuine `text` block (a pasted image + prompt).

    The verdict relay (hook._relay_guard) re-invokes the *assistant* with no new user
    message, so several relay iterations share one user turn. Anchoring only on the user
    message would hand each re-check every earlier iteration's prose — and
    tool_call_provenance would keep re-failing prose an earlier iteration already
    answered, echoing a verdict the agent's own fix has since made stale. A Tycho
    `stop_hook_summary` entry marks where the previous iteration was verified, so it
    opens the next iteration's turn exactly as a user message opens the first. The
    current Stop's own summary is written *after* this runs, so `max` lands on the
    previous boundary, never this one.

    Returns 0.0 when nothing is found — "the whole transcript is the turn", which is
    both the single-turn case and the honest fallback.
    """
    starts = [
        _epoch(e.get("timestamp"))
        for e in _entries(transcript)
        if _is_user_prose(e) or _is_relay_boundary(e)
    ]
    return max(starts, default=0.0)


# A relay re-check is a Tycho Stop verdict fed back to the agent (hook._relay_guard). The
# entry is a `stop_hook_summary` whose injected context carries this fixed guard sentence —
# stable across the "re-check N of M" / "final re-check" variants and unique to the relay, so
# it can't collide with an honest assistant line. `hookAdditionalContext` is a list of strings.
_RELAY_BOUNDARY = "automated verification of the turn you just finished"


def _is_relay_boundary(entry: dict) -> bool:
    """True for a Tycho verdict-relay Stop summary — the boundary that opens the next
    relay iteration's turn, so its prose is judged on its own tool calls (not the last one's)."""
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

    Cursor's Stop transcript is thinner than Claude's: each ``tool_use`` block
    carries only ``name`` + ``input`` — no ids, no timestamps, and no
    ``tool_result`` blocks at all. So Events get ts=0.0, is_error=None,
    result={}, and checks that need timings or exit codes (test_freshness,
    command_execution) degrade to UNSUPPORTED honestly for Cursor sessions.
    Verified against a real transcript at ``tests/fixtures/cursor_transcript_sample.jsonl``.
    """
    events = []
    for entry in _entries(transcript):
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("name"):
                tool = {"Shell": "Bash", "StrReplace": "Edit"}.get(block["name"], block["name"])
                events.append(Event(ts=0.0, tool=tool, input=block.get("input") or {}))
    return tuple(events)


def parse_codex(transcript: Path) -> tuple[Event, ...]:
    """Read *every* turn from a Codex rollout JSONL transcript.

    Returns all turns' events, not just the latest, so the session-scoped checks can
    reason across turns (freshness/provenance, and the AST checks that diff the earliest
    original against disk); the Stop narrows to the turn under review via
    ``Harness.turn_start`` (``turn_start_codex``), exactly as Claude does. (TYCHO-20 —
    this used to filter to the latest ``turn_id`` in the reader, which left those checks
    blind to earlier turns and made Codex's scope disagree with every other reader.)
    """
    calls: dict[str, tuple[float, str]] = {}
    results: dict[str, tuple[bool | None, str]] = {}  # call_id -> (is_error, output text)
    events = []
    for entry in _entries(transcript):
        payload = entry.get("payload") or {}
        ts = _epoch(entry.get("timestamp"))
        if entry.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
            command = _codex_command(payload.get("input"))
            if command:
                calls[payload.get("call_id", "")] = (ts, command)
        elif entry.get("type") == "response_item" and payload.get("type") == "custom_tool_call_output":
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
            # Keep the runner's own words, not just the verdict we distilled from them.
            # `is_error` alone is useless the moment the shell masks the status: the engine
            # then has to re-read the output, and it can only read what the reader kept
            #. Codex is the one harness whose transcript carries this text.
            result={"stdout": out} if (out := results.get(call_id, (None, ""))[1]) else {},
        )
        for call_id, (ts, command) in calls.items()
    )
    return tuple(sorted(events, key=lambda e: e.ts))


def assistant_messages_codex(transcript: Path) -> tuple[Message, ...]:
    """Codex assistant prose from response-item messages.

    Codex also mirrors these as ``event_msg.agent_message`` entries; reading only the
    response item avoids counting every claim twice.
    """
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


def turn_start_codex(transcript: Path) -> float:
    """Epoch of the latest Codex turn's ``task_started`` — the boundary its Stop reviews.

    ``parse_codex`` now returns every turn, so the engine narrows to the turn
    under review via ``turn_start`` just like Claude. We anchor on the latest turn that
    actually has tool calls or edits: Codex emits empty trailing turns (compaction
    follow-ups), and anchoring on one of those would push the boundary past all real work
    and blank out ``turn_edits``. 0.0 when there is no such turn — the honest "the whole
    transcript is the turn" fallback.
    """
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
    """Timestamp of a Tycho continuation prompt, or 0.

    Codex keeps relay iterations inside the same ``turn_id``. Its rejected Stop is written
    back as a user ``<hook_prompt>`` response item, so that injection—not ``task_started``—
    opens the next iteration.
    """
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
    text = Path(transcript).read_text(encoding="utf-8")
    return json.loads(text[text.index("{"):])


def parse_opencode(transcript: Path) -> tuple[Event, ...]:
    """Read an OpenCode session JSON (``{info, messages:[{parts:[…]}]}``).

    Tycho rebuilds this shape from ``opencode.db`` (see ``opencode.py``); it's
    identical to what ``opencode export`` emits, so this reader is unchanged.
    """
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
    """Epoch of the OpenCode session's last user message — the boundary its Stop reviews.

    An OpenCode turn runs `user message -> assistant messages/tool parts`, so a user
    message *starts* the next turn; the last one opens the turn the Stop fires on. Snitch
    reads the same store and treats a user line the same way (`reader_opencode.go`).

    OpenCode timestamps in ms, but ``parse_opencode`` divides by 1000 to give ``Event.ts``
    in seconds — so the boundary is scaled to match. A ms boundary against second-scale
    events would sit ~1000x in the future and silently blank out ``turn_edits``, which is
    the quiet failure this scoping exists to prevent.

    Returns 0.0 when no user message carries a timestamp — the same honest "the whole
    transcript is the turn" fallback as the other readers.
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
            if e.get("type") == "response_item" and p.get("type") in ("custom_tool_call",):
                return tid
            if e.get("type") == "event_msg" and p.get("type") == "patch_apply_end":
                return tid
    return None


def _codex_command(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    start = value.find('{cmd:"')
    if start < 0:
        return None
    start += 6
    for delim in ('","workdir"', '",', '"}', '")'):
        end = value.find(delim, start)
        if end >= 0:
            return value[start:end]
    if value.endswith('"') and start < len(value) - 1:
        return value[start:-1]
    return None


def _codex_output_text(value: object) -> str:
    """Flatten a Codex tool-call output into the text the command actually printed.

    Codex nests it (`[{"type": "input_text", "text": "77 passed in 0.79s"}]`), so pull the
    `text` leaves out and leave the scaffolding behind. Normalizing it into `result` is
    what lets the harness-agnostic engine re-read a runner's verdict when the shell masked
    the exit status — the reader is the only layer allowed to know this shape.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(text := value.get("text"), str):
            return text
        return "\n".join(t for v in value.values() if (t := _codex_output_text(v)))
    if isinstance(value, list):
        return "\n".join(t for item in value if (t := _codex_output_text(item)))
    return ""


def _codex_is_error(value: object) -> bool | None:
    if isinstance(value, dict):
        if isinstance(value.get("exit_code"), int):
            return value["exit_code"] != 0
        return next((r for item in value.values() if (r := _codex_is_error(item)) is not None), None)
    if isinstance(value, list):
        return next((r for item in value if (r := _codex_is_error(item)) is not None), None)
    if isinstance(value, str):
        match = re.search(r'"exit_code"\s*:\s*(\d+)', value)
        if match:
            return int(match.group(1)) != 0
        # Current Codex rollouts omit the exec exit code, so fall back to the runner's own
        # summary — the same reading `checks` does for a shell-masked status, shared via
        # `runlog` so one runner's output format is described in exactly one place.
        # ("Script completed" is deliberately not success: it appears for failed commands.)
        return runlog.outcome(value)
    return None


def file_edits(events: tuple[Event, ...]) -> tuple[FileEdit, ...]:
    """Project the *successful* Edit/Write events into FileEdits (path, ts, before-content).

    Reads Claude's ``file_path``/``filePath`` or Cursor's ``path`` input key.

    A failed call is not evidence of an edit: one denied by a PreToolUse hook, blocked, or
    errored never touched the disk, and — having no ``toolUseResult`` — would land here as a
    phantom ``kind=create, original=None`` row. ``is_error is None`` means *no
    status was recorded*, not failure, so those are still counted; Cursor never records one
    and would otherwise report zero edits.
    """
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


def _epoch(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0

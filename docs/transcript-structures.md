# Transcript Structures — Claude Code / Cursor / Codex / OpenCode

> How each harness records what an agent did, and what that means for verification.

## Quick comparison

| | Claude Code | Cursor | Codex | OpenCode |
|---|---|---|---|---|
| Format | JSONL (one JSON object per line) | JSONL | JSONL | SQLite `opencode.db` (Tycho rebuilds to JSON) |
| Event pairing | `tool_use.id` ↔ `tool_result.tool_use_id` | None — `tool_use` only | `custom_tool_call.call_id` ↔ `custom_tool_call_output.call_id` | Nested inside message parts |
| Timestamps | ISO 8601 per entry, ~second precision | None | ISO 8601 per entry | ms epoch per part (start + end) |
| Exit codes | `tool_result.is_error` (bool) | None | In output text (parsed from script output) | `metadata.exit` (int) or `status == "error"` |
| Tool names | `"Bash"`, `"Edit"`, `"Write"` | PascalCase: `"Shell"`, `"Write"`, `"Read"`, `"Glob"`, `"Grep"` | `"exec"` (only shell — edits are `patch_apply_end` events) | Lowercase: `"bash"`, `"edit"`, `"write"`, `"read"`, `"glob"`, `"grep"` |
| Before-content for edits | Yes — `toolUseResult.originalFile` | No | No (only file paths and patch type) | No |
| Turn scoping | Full session (reader reads all; engine narrows to the last turn via `turn_start`) | Single turn per transcript | Multi-turn rollout (reader returns all; engine narrows via `turn_start`) | Full session (reader reads all; engine narrows to the last turn via `turn_start`) |
| Storage location | `~/.claude/projects/<enc>/<uuid>.jsonl` | `~/.cursor/projects/<enc>/agent-transcripts/<id>/<id>.jsonl` | `~/.codex/sessions/<year>/<month>/<day>/rollout-*.jsonl` | `~/.local/share/opencode/opencode.db` (SQLite; `session`/`message`/`part` tables) |

---

## Claude Code

A JSONL file where each line is one entry in the conversation. Tool calls and results are
separate entries paired by ID. A Stop hook fires after each turn; the hook payload includes
the transcript path.

### Entry types

```jsonl
{"type":"system","timestamp":"…","content":"session start"}
{"type":"assistant","timestamp":"…","message":{"role":"assistant","content":[
  {"type":"text","text":"Running lint."},
  {"type":"tool_use","id":"toolu_lint","name":"Bash","input":{"command":"ruff check","description":"Lint"}}
]}}
{"type":"user","timestamp":"…","toolUseResult":{"stdout":"…","stderr":"","interrupted":false},
  "message":{"role":"user","content":[
    {"type":"tool_result","tool_use_id":"toolu_lint","content":"blocked by hook","is_error":true}
  ]}}
```

| Field | Description |
|---|---|
| `message.content[].type: "tool_use"` | A tool was invoked. Has `id`, `name`, `input`. |
| `message.content[].type: "tool_result"` | The result of a tool invocation. References `tool_use_id`. Has `is_error` (bool). |
| `message.content[].type: "text"` | Free text. Ignored by Tycho. |
| `entry.toolUseResult` | Outside `content[]`, carries structured output on user entries. For Bash: `stdout`/`stderr`. For Edit: `originalFile` (before-content). For Write: `content` (before-content was null). |
| `entry.timestamp` | ISO 8601 string. Both the tool_use and tool_result get separate timestamps. |

### How Tycho reads it

`events.parse()` tracks `tool_use` blocks by `id` in a dict, pairs them with `tool_result`
blocks by `tool_use_id`, and builds one `Event` per use-result pair with the result's
timestamp, the exit/error signal, and the structured result.

**Strengths:** real timestamps → freshness works; `is_error` bool → command_execution works;
`originalFile` → assertion_weakening and skip_mock_injection work.

---

## Cursor

A JSONL file where each line represents one assistant message (which may contain multiple
tool_use blocks). Tool results are **not recorded** in the transcript — no IDs, no
timestamps, no is_error.

Cursor's Stop transcript fires after each turn and is structurally similar to Claude's
format (uses `message.content[]` blocks), but with no result entries.

### Entry types

```jsonl
{"role":"user","message":{"content":[
  {"type":"text","text":"<user_query>\n...\n</user_query>"}
]}}
{"role":"assistant","message":{"content":[
  {"type":"text","text":"I'll look into this."},
  {"type":"tool_use","name":"Glob","input":{"glob_pattern":"**/*.py"}},
  {"type":"tool_use","name":"Read","input":{"path":"/repo/src/main.py","limit":80}}
]}}
{"type":"turn_ended","status":"success"}
```

| Field | Description |
|---|---|
| `message.content[].type: "tool_use"` | A tool was invoked. **No `id` field.** Has `name` (PascalCase) and `input`. |
| `message.content[].tool_use.name` | Tool names: `"Shell"`, `"Write"`, `"Read"`, `"Glob"`, `"Grep"`, `"StrReplace"`, `"Edit"`, `"Bash"`, etc. |
| `message.content[].tool_use.input` | Tool input. For `Write`: `{"path": "…", "contents": "…"}`. For `Shell`/`Bash`: `{"command": "…"}`. For `Read`: `{"path": "…"}`. |
| `entry.type: "turn_ended"` | Marks the end of a turn (Cursor's Stop). |
| Timestamps | **None.** No timestamps on any entry. |

### How Tycho reads it

`events.parse_cursor()` maps each `tool_use` block directly to an `Event`, mapping:
- `"Shell"` → `"Bash"`
- `"StrReplace"` → `"Edit"`
- Everything else passes through as-is

Events get `ts=0.0`, `is_error=None`, `result={}`.

Because no timestamps or results exist, freshness, provenance, and command_execution
degrade to UNSUPPORTED. File_state and git_state still work (they check disk/git).

---

## Codex

A JSONL rollout file containing one conversation (possibly multiple turns). The Stop hook
fires after each turn and the payload includes the rollout path.

Codex does not format commands as standalone tool-use objects — instead it wraps shell
invocations inside a JavaScript `exec_command` call sent to Codex's internal runtime.

### Entry types

```jsonl
{"timestamp":"…","type":"session_meta","payload":{"session_id":"…","cwd":"/repo","originator":"codex-tui"}}
{"timestamp":"…","type":"event_msg","payload":{"type":"task_started","turn_id":"turn-abc"}}
{"timestamp":"…","type":"response_item","payload":{
  "type":"custom_tool_call","call_id":"call-1","name":"exec",
  "input":"const r = await tools.exec_command({cmd:\"pytest -q\",workdir:\"/repo\"}); text(JSON.stringify(r))",
  "internal_chat_message_metadata_passthrough":{"turn_id":"turn-abc"}
}}
{"timestamp":"…","type":"response_item","payload":{
  "type":"custom_tool_call_output","call_id":"call-1",
  "output":[{"type":"input_text","text":"Script completed\nWall time 0.8 seconds\nOutput:\n77 passed in 0.79s\n"}],
  "internal_chat_message_metadata_passthrough":{"turn_id":"turn-abc"}
}}
{"timestamp":"…","type":"event_msg","payload":{
  "type":"patch_apply_end","call_id":"patch-1","turn_id":"turn-abc","success":true,
  "changes":{"/repo/docs/new.md":{"type":"add","content":"new"}}
}}
{"timestamp":"…","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-abc"}}
```

| Field | Description |
|---|---|
| `session_meta` | Start of a rollout. Contains `cwd`. |
| `event_msg.payload.type: "task_started"` | Start of a new turn. |
| `event_msg.payload.type: "task_complete"` | End of a turn. |
| `response_item.payload.type: "custom_tool_call"` | A shell command call. Has `call_id`, `name: "exec"`, `input` (JS snippet wrapping `exec_command`). |
| `response_item.payload.type: "custom_tool_call_output"` | Result of a call. Has `call_id`, `output` (list of text blocks; exit status is parsed from the text). |
| `event_msg.payload.type: "patch_apply_end"` | A file edit that was applied to disk. Has `changes` (path → `{type: "add"|"update"|"remove", content}`). `success: true` means it applied. |
| Turn scoping | All entries carry `turn_id` (or `internal_chat_message_metadata_passthrough.turn_id`). |

### How Tycho reads it

`events.parse_codex()` returns **every** turn's events (TYCHO-20 — it used to filter to the latest
turn, which blinded the session-scoped checks to earlier turns):
1. Extracts commands from `custom_tool_call.input` by parsing the `exec_command({cmd:…})` JS wrapper.
2. Pairs calls with results by `call_id`.
3. Converts successful `patch_apply_end.changes` entries to `Event(tool="Write", input={"path": …})`.

`events.turn_start_codex()` then reports the boundary of the turn under review — the `task_started`
timestamp of the **latest turn that has actual events** (`custom_tool_call` or `patch_apply_end`),
skipping empty trailing turns (compaction follow-ups). The engine narrows the turn-scoped checks to
that boundary via `session.turn_events`/`turn_edits`, exactly as it does for Claude.

**Exit status:** Not directly recorded. Tycho reads an `exit_code` field when the output carries one,
and otherwise falls back to pytest's terminal summary (`N passed … in Xs` → pass, `N failed`/`=== ERRORS ===`
→ fail). "Script completed" is deliberately *not* treated as success — it appears for failed commands too.
When nothing conclusive is found, command_execution reports UNSUPPORTED.

**Before-content:** None recorded for patches. AST-diff checks degrade to UNSUPPORTED.

---

## OpenCode

OpenCode persists every session in a SQLite database at
`~/.local/share/opencode/opencode.db` (tables `session`, `message`, `part`). Tycho reads that DB
directly — read-only — and rebuilds the session JSON below from its `message` and `part` rows: one
message in, one message out, each message's parts grouped under it by `part.message_id`.

> **Why not `opencode export`?** The CLI *can* serialize a session to JSON, but it truncates its
> output at a fixed 128 KB whenever stdout is a pipe (a Bun non-blocking-write quirk); only a file
> redirect gets the full payload. A hook captures stdout through a pipe, so every non-trivial session
> came back as invalid JSON and the verdict was silently dropped. Reading the DB avoids the CLI
> entirely and matches how the other three harnesses work: read the harness's own on-disk store.

Each `part.data` row is exactly the object `opencode export` nests under `messages[].parts[]`, so the
reconstructed JSON is identical to the export format and `events.parse_opencode` is unchanged. A
message's `info` keeps its `role` and `time` — the latter is what `turn_start_opencode` anchors the
turn boundary on. The rest of `message.data` is dropped: a user message hangs full patches off
`summary.diffs`, and nothing downstream reads them.

### Session format (reconstructed)

```json
{
  "info": {"id": "ses_…", "directory": "/repo", "version": "1.17.20"},
  "messages": [
    {
      "info": {"role": "user", "time": {"created": 1784054110000}},
      "parts": []
    },
    {
      "info": {"role": "assistant", "time": {"created": 1784054112000}},
      "parts": [
        {"type": "tool", "tool": "bash", "state": {
          "status": "completed",
          "input": {"command": "pytest -q"},
          "output": "1 passed",
          "metadata": {"exit": 0},
          "time": {"start": 1784054120000, "end": 1784054121000}
        }},
        {"type": "tool", "tool": "edit", "state": {
          "status": "completed",
          "input": {"filePath": "/repo/src/app.py", "oldString": "x = 1", "newString": "x = 2"},
          "metadata": {},
          "time": {"start": 1784054112244, "end": 1784054112246}
        }}
      ]
    }
  ]
}
```

| Field | Description |
|---|---|
| `info.id` | Session ID (matches the `session.idle` event). |
| `info.directory` | Working directory of the session. |
| `messages[].info.role` | `"user"` or `"assistant"`. A user message opens the next turn. |
| `messages[].info.time.created` | Milliseconds since Unix epoch. The last user message's is the turn boundary (`turn_start_opencode`, scaled to seconds). |
| `messages[].parts[].type: "tool"` | A tool invocation. Only `"tool"` type is extracted (text parts are ignored). |
| `part.tool` | Lowercase: `"bash"`, `"edit"`, `"write"`, `"read"`, `"glob"`, `"grep"`, etc. |
| `part.state.input` | Tool input arguments. |
| `part.state.status` | `"completed"`, `"error"`, etc. |
| `part.state.metadata.exit` | Exit code for bash (int). Present and `0` for success, present and non-zero for failure, absent/missing for no information. |
| `part.state.time.start/end` | Milliseconds since Unix epoch. |

### How Tycho reads it

`opencode.py` (the DB source):
1. `latest_session(cwd)` → `SELECT id FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1`
   (on-disk discovery, exactly like the other three harnesses).
2. `session_json(id, cwd)` → reads the session's `message` rows (keeping each one's `role` and
   `time`) and its `part` rows (keeping `data.type == "tool"`), grouping parts under their parent by
   `part.message_id`, as `{info, messages:[{info, parts:[…]}]}`. A user message survives with
   `parts: []` — its timestamp is the turn boundary.
3. `materialize(...)` writes that JSON to a temp file that the hook/verify caller hands to the engine
   and then unlinks.

`events.parse_opencode()` (unchanged):
1. Parses the JSON.
2. Iterates all messages and their parts.
3. Only processes parts with `type === "tool"`.
4. Maps lowercase tool names: `"bash"` → `"Bash"`, `"edit"` → `"Edit"`, `"write"` → `"Write"`.
5. Computes `is_error` from `metadata.exit` (int) or `status === "error"`.
6. Converts ms timestamps to seconds via `/ 1000`.

**Before-content:** Edit parts *do* carry `oldString`/`newString` (and a full unified `diff` in
`metadata`), but Tycho's FileEdit projection doesn't consume `oldString` today, so the AST-diff checks
still degrade to UNSUPPORTED. Wiring `oldString` is a possible future upgrade.

**Exit codes:** Available as `metadata.exit` when the shell process exits, meaning
command_execution can work when present.

**Timestamps:** Available (ms epoch, from `state.time`) → freshness and provenance can work.

---

## Honest degradation summary

| Check | Claude | Cursor | Codex | OpenCode |
|---|---|---|---|---|
| command_execution | ✅ `is_error` in result | ⚪ no exit codes | ⚪ exit status parsed from text (UNSUPPORTED when unreachable) | ✅ `metadata.exit` when present |
| test_freshness | ✅ result timestamp vs file mtime | ⚪ no timestamps | ✅ turn timestamps | ✅ part timestamps |
| test_provenance | ✅ edit timestamp vs test run | ⚪ no timestamps | ✅ edit timestamp vs test run | ✅ part timestamps |
| assertion_weakening | ✅ `originalFile` in result | ⚪ no before-content | ⚪ no before-content | ⚪ `oldString` present but not yet consumed |
| skip_mock_injection | ✅ `originalFile` in result | ⚪ no before-content | ⚪ no before-content | ⚪ `oldString` present but not yet consumed |
| file_state | ✅ | ✅ | ✅ | ✅ |
| git_state | ✅ | ✅ | ✅ | ✅ |
| scope_drift | ✅ | ✅ | ✅ | ✅ |

---

## Quick reference: tool name mapping

| Canonical (Tycho Event) | Claude | Cursor | Codex | OpenCode |
|---|---|---|---|---|
| `Bash` | `Bash` | `Shell` or `Bash` | `exec` (via `custom_tool_call`) | `bash` |
| `Edit` | `Edit` | `StrReplace` or `Edit` | — | `edit` |
| `Write` | `Write` | `Write` | `patch_apply_end.changes` | `write` |
| Others | pass through | pass through | — | pass through |

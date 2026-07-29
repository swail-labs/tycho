"""Stop hooks + Claude Code, Cursor, and Codex adapters."""

import io
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tycho.engine import checks
from tycho.read import events, harness
from tycho.wire import hook
from tycho.wire import install as init_mod
from tycho.read import opencode
from tycho.read import session as engine
from tycho.store.config import Config
from tycho.model import CheckStatus, Event, FileEdit, FileState, GitSnapshot, Session

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "transcript_sample.jsonl"
CURSOR_FIXTURE = FIXTURES / "cursor_transcript_sample.jsonl"
CODEX_FIXTURE = FIXTURES / "codex_transcript_sample.jsonl"
CODEX_PIN_FIXTURE = FIXTURES / "codex_attribution.jsonl"
OPENCODE_FIXTURE = FIXTURES / "opencode_transcript_sample.json"
CURSOR_STOP_PAYLOAD = FIXTURES / "harness" / "cursor" / "stop_payload.json"


# --- harness detection + adapters -------------------------------------------

def test_detect_claude_by_default():
    assert harness.detect({"cwd": "/repo"}).name == "claude"


def test_detect_cursor_by_workspace_roots():
    assert harness.detect({"workspace_roots": ["/repo"]}).name == "cursor"


def test_detect_codex_by_stop_payload():
    payload = {"hook_event_name": "Stop", "turn_id": "turn-1", "cwd": "/repo"}
    assert harness.detect(payload).name == "codex"


def test_repo_root_from_cwd_vs_workspace_roots():
    assert harness.CLAUDE.repo_root({"cwd": "/a"}) == Path("/a")
    assert harness.CURSOR.repo_root({"workspace_roots": ["/b"]}) == Path("/b")


def test_output_channels_differ():
    assert "systemMessage" in harness.CLAUDE.format_output("x")
    assert "followup_message" in harness.CURSOR.format_output("x")


# --- cursor Stop payload (pinned to cursor-agent 2026.07.09-a3815c0) ---------
#
# The adapter used to *infer* Cursor's field names. These pin them to the shipped contract
# (payload keys from executeHookForStep, the output key from the stop validator), so a
# rename fails here rather than in a user's dead hook.

def test_cursor_stop_payload_is_detected_as_cursor():
    payload = json.loads(CURSOR_STOP_PAYLOAD.read_text())
    assert harness.detect(payload).name == "cursor"


def test_cursor_stop_payload_yields_repo_root_and_transcript():
    payload = json.loads(CURSOR_STOP_PAYLOAD.read_text())
    assert harness.CURSOR.repo_root(payload) == Path("/Users/me/projects/tycho")
    assert harness.CURSOR.transcript_of(payload) == Path(payload["transcript_path"])


def test_cursor_stop_output_carries_only_followup_message():
    # Cursor's stop validator reads `followup_message` and nothing else — any other
    # key is silently dropped, which is how the old `user_message` reached nobody.
    out = harness.CURSOR.format_output("Tycho: PASSED")
    assert list(out) == ["followup_message"]
    assert isinstance(out["followup_message"], str)  # validator: must be a string


def test_cursor_followup_leads_with_verdict_then_tells_model_to_stop():
    # Cursor replays the verdict into the model loop, so it has to carry its own stop
    # condition — otherwise a FAILED verdict reads as "go fix this" and the agent works on.
    out = harness.CURSOR.format_output("Tycho: FAILED — tests did not run")["followup_message"]
    assert out.startswith("Tycho: FAILED — tests did not run")  # verdict first, never buried
    assert "end your turn now" in out and "verbatim" in out


# --- cursor transcript reader (pinned to a real transcript) -----------------

def test_cursor_reader_extracts_tool_use_without_ids_or_results():
    evs = events.parse_cursor(CURSOR_FIXTURE)
    assert evs, "expected tool_use events from the Cursor transcript"
    assert all(e.tool for e in evs)
    assert all(e.is_error is None and e.result == {} for e in evs)  # thin: no results
    assert any(e.tool == "Read" for e in evs)


def test_cursor_write_projects_a_file_edit():
    # Cursor's edit tool is `Write` with an `input.path` key (no result/original).
    edits = events.file_edits(events.parse_cursor(CURSOR_FIXTURE))
    assert any(e.path.endswith("distribution-ideas.md") for e in edits)


def test_codex_reader_returns_every_turn_and_extracts_commands_and_edits():
    """: the reader no longer discards earlier turns — turn-old's edit is back."""
    evs = events.parse_codex(CODEX_FIXTURE)
    edits = events.file_edits(evs)
    assert [e.input["command"] for e in evs if e.tool == "Bash"] == ["pytest -q"]
    assert next(e for e in evs if e.tool == "Bash").is_error is False
    # Every turn's edits, including turn-old's /repo/old.md (was filtered out before).
    assert {e.path for e in edits} == {"/repo/old.md", "/repo/docs/new.md", "/repo/src/app.py"}


def test_codex_reader_extracts_assistant_prose_once(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-14T18:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "I searched the web."},
        },
        {
            "timestamp": "2026-07-14T18:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I searched the web."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert [m.text for m in events.assistant_messages_codex(transcript)] == ["I searched the web."]


def test_codex_turn_start_is_the_latest_event_bearing_turn():
    # turn-current's task_started, not turn-old's, and not 0.0.
    assert events.turn_start_codex(CODEX_FIXTURE) == events._epoch("2026-07-14T18:01:00.000Z")


def test_codex_relay_prompt_opens_a_new_iteration(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-14T18:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-07-14T18:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "input": 'tools.exec_command({cmd:"pytest -q","workdir":"/repo"})',
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
        {
            "timestamp": "2026-07-14T18:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        '<hook_prompt hook_run_id="stop:1">[TYCHO] The above is an '
                        "automated verification of the turn you just finished</hook_prompt>"
                    ),
                }],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert events.turn_start_codex(transcript) == events._epoch("2026-07-14T18:00:02.000Z")


def test_codex_turn_scoping_narrows_edits_but_not_the_session():
    """Session sees all turns; the turn-scoped view is just turn-current's edits.

    Paths are repo-relative POSIX here, not the reader's absolute ones: `gather()` runs every
    FileEdit through `verify._relpath` so git and the `[scope]` globs can reconcile.
    """
    session = engine.gather(
        CODEX_FIXTURE, Path("/repo"), parse=events.parse_codex, turn_start=events.turn_start_codex
    )
    assert "old.md" in {fe.path for fe in session.edits}          # session scope keeps it
    assert "old.md" not in {fe.path for fe in session.turn_edits}  # turn scope drops it
    assert {fe.path for fe in session.turn_edits} == {"docs/new.md", "src/app.py"}


def test_codex_session_scope_catches_a_source_uncovered_since_an_earlier_green_run():
    """ acceptance: green run in turn 1, source edited in turn 2, never retested.

    parse_codex used to discard turn 1, so test_freshness saw no green run and stayed
    UNSUPPORTED. With every turn returned, the earlier green run is visible and the
    later, untested source edit reports STALE."""
    evs = events.parse_codex(CODEX_FIXTURE)
    # Repo-relative, as `gather()` makes them: a path left absolute reads as *outside* /repo,
    # which is `scope_drift`'s business and not staleness.
    edits = tuple(replace(e, path=engine._relpath(e.path, Path("/repo")))
                  for e in events.file_edits(evs))
    app = next(e.path for e in edits if e.path.endswith("src/app.py"))
    green = events._epoch("2026-07-14T18:00:02.000Z")  # the turn-old pytest run
    session = Session(
        events=evs,
        edits=edits,
        repo=Path("/repo"),
        config=Config(),
        files={app: FileState(path=app, exists=True, mtime=green + 10, current_text="print('new')\n")},
        git=GitSnapshot(False, None, ()),
        turn_start=events.turn_start_codex(CODEX_FIXTURE),
    )
    assert checks._last_green_run_ts(session) == green            # the earlier turn's run is visible
    assert checks.test_freshness(session).status is CheckStatus.STALE


def _codex_function_call_rows() -> list[dict]:
    """A turn in Codex's structured shell shape: `function_call`, not `custom_tool_call`."""
    return [
        {
            "timestamp": "2026-07-23T21:22:31.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-07-23T21:22:32.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_a",
                "arguments": json.dumps({
                    "cmd": "pytest -q",
                    "workdir": "/repo",
                    "yield_time_ms": 10000,
                }),
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
        {
            "timestamp": "2026-07-23T21:22:34.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_a",
                "output": "Wall time: 1.1 seconds\nProcess exited with code 0\nOutput:\n77 passed in 1.07s\n",
            },
        },
        # Session plumbing, not a command: no `cmd`, so nothing to attribute a run to.
        {
            "timestamp": "2026-07-23T21:22:35.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "write_stdin",
                "call_id": "call_b",
                "arguments": json.dumps({"session_id": 50408, "chars": "y\n"}),
            },
        },
        {
            "timestamp": "2026-07-23T21:22:36.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait",
                "call_id": "call_c",
                "arguments": json.dumps({"cell_id": "30", "yield_time_ms": 10000}),
            },
        },
    ]


def _write_rows(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_codex_reader_extracts_commands_from_the_structured_shell_shape(tmp_path: Path):
    """Codex runs the shell through `function_call name:"exec_command"` as well as the
    freeform `custom_tool_call name:"exec"`. Reading only the latter made Tycho blind to
    entire sessions — every command invisible, so the Stop found no verifiable activity
    and said nothing at all."""
    transcript = _write_rows(tmp_path / "rollout.jsonl", _codex_function_call_rows())
    evs = events.parse_codex(transcript)
    assert [e.input["command"] for e in evs if e.tool == "Bash"] == ["pytest -q"]


def test_codex_reader_ignores_function_calls_that_carry_no_command(tmp_path: Path):
    # `wait` and `write_stdin` drive an already-running session; counting them as commands
    # would invent runs that never happened.
    transcript = _write_rows(tmp_path / "rollout.jsonl", _codex_function_call_rows())
    assert len([e for e in events.parse_codex(transcript) if e.tool == "Bash"]) == 1


def test_codex_turn_start_anchors_on_a_structured_shell_turn(tmp_path: Path):
    # The turn is only "real" if the reader can see its events; before exec_command was
    # read, this turn looked empty and the boundary fell back to 0.0.
    transcript = _write_rows(tmp_path / "rollout.jsonl", _codex_function_call_rows())
    assert events.turn_start_codex(transcript) == events._epoch("2026-07-23T21:22:31.000Z")


def _exec_call(input_text: str, name: str = "exec") -> dict:
    return {"type": "custom_tool_call", "call_id": "c", "name": name, "input": input_text}


def test_codex_command_reads_both_spellings_of_the_cmd_key():
    # Codex emits the JS object literal either way; scanning for one of them dropped 49 of
    # the exec calls in a single real session.
    unquoted = _exec_call('const r = await tools.exec_command({cmd:"pytest -q",workdir:"/repo"})')
    quoted = _exec_call('const r = await tools.exec_command({"cmd":"pytest -q","workdir":"/repo"})')
    assert events._codex_command_of(unquoted) == "pytest -q"
    assert events._codex_command_of(quoted) == "pytest -q"


def test_codex_command_survives_an_escaped_quote_inside_the_command():
    # Guessing the closing delimiter truncated the command at the first inner quote, so a
    # `python3 -c "..."` run reached the checks as a fragment of itself.
    call = _exec_call(
        r'const r = await tools.exec_command({"cmd":"python3 -c \"import sys; print(1)\" && pytest -q","workdir":"/repo"})'
    )
    assert events._codex_command_of(call) == 'python3 -c "import sys; print(1)" && pytest -q'


def test_codex_command_ignores_a_non_exec_tool_whose_payload_mentions_cmd():
    # `apply_patch` carries patch text, which can contain anything — including the literal
    # this scanner keys on. A patch is not a command, and inventing one fabricates evidence
    # that a run happened.
    patch = _exec_call('*** Begin Patch\n+run({cmd:"pytest -q"})\n*** End Patch', name="apply_patch")
    assert events._codex_command_of(patch) is None


def test_codex_is_exposed_in_normal_usage(tmp_path: Path):
    """The gate that kept Codex out of `discover`, `init` and the CLI choices. Everything
    behind it — reader, installer, relay — was already built and tested."""
    assert "codex" in harness.ENABLED_NAMES
    assert harness.CODEX in harness.ENABLED
    (tmp_path / ".codex").mkdir()
    assert "codex" in init_mod.detect(tmp_path)


def test_discover_finds_a_codex_session_for_this_repo(tmp_path, monkeypatch):
    # Discovery is what `tycho verify` with no arguments and `doctor` both run on.
    root = tmp_path / "sessions" / "2026" / "07" / "23"
    root.mkdir(parents=True)
    rollout = root / "rollout-2026-07-23T22-09-51.jsonl"
    rollout.write_text(
        json.dumps({
            "timestamp": "2026-07-23T22:09:57.512Z",
            "type": "session_meta",
            "payload": {"session_id": "s-1", "cwd": str(tmp_path), "cli_version": "0.145.0"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYCHO_CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    path, found = harness.discover(tmp_path, only="codex")
    assert path == rollout and found.name == "codex"


def test_codex_readers_hold_against_the_pinned_version(tmp_path: Path):
    """The whole adapter contract against a transcript in the pinned version's shape — the
    claim `VERIFIED_AGAINST["codex"]` makes. Reading one field is what the pin is for; this
    reads every field the hook depends on, so the pin can only move when data moves with it.
    """
    evs = events.parse_codex(CODEX_PIN_FIXTURE)
    runs = [e for e in evs if e.tool == "Bash"]
    assert [e.input["command"] for e in runs] == ["pytest -q"]
    # The exit status is what `command_execution` reads; it going missing is the silent
    # failure this pin exists to catch.
    assert runs[0].is_error is False
    assert "77 passed" in (runs[0].result.get("stdout") or "")
    assert {e.path for e in events.file_edits(evs)} == {"/repo/app.py"}
    assert events.turn_start_codex(CODEX_PIN_FIXTURE) == events._epoch("2026-07-23T22:09:59.000Z")
    assert [m.text for m in events.assistant_messages_codex(CODEX_PIN_FIXTURE)] == [
        "Added the helper and ran the suite."
    ]
    got = events.attribution_codex(CODEX_PIN_FIXTURE)
    assert (got.model, got.agent_version) == ("gpt-5.6-sol", "0.145.0")
    assert got.session_id


def test_codex_attribution_reads_model_version_and_session(tmp_path: Path):
    """Codex records all three, in two places: `session_meta` carries the session id and the
    CLI version, `turn_context` carries the model. Storing nulls made every Codex turn
    "unknown (codex)" in the decay ledger, which slices catch rate by model."""
    rows = [
        {
            "timestamp": "2026-07-23T22:09:57.512Z",
            "type": "session_meta",
            "payload": {
                "session_id": "019f9107-2220-75c2-9089-1fc7b6755746",
                "cwd": "/repo",
                "cli_version": "0.145.0",
            },
        },
        {
            "timestamp": "2026-07-23T22:10:00.000Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "cwd": "/repo", "model": "gpt-5.6-sol"},
        },
    ]
    got = events.attribution_codex(_write_rows(tmp_path / "rollout.jsonl", rows))
    assert got.session_id == "019f9107-2220-75c2-9089-1fc7b6755746"
    assert got.agent_version == "0.145.0"
    assert got.model == "gpt-5.6-sol"


def test_codex_attribution_takes_the_last_model_of_a_resumed_session(tmp_path: Path):
    # A session can be resumed under a different model; the turn being verified ran under the
    # latest one. Same rule as the Claude reader.
    rows = [
        {"timestamp": "2026-07-23T22:09:57.512Z", "type": "session_meta",
         "payload": {"session_id": "s-1", "cli_version": "0.145.0"}},
        {"timestamp": "2026-07-23T22:10:00.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-07-23T22:20:00.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol-high"}},
    ]
    assert events.attribution_codex(_write_rows(tmp_path / "r.jsonl", rows)).model == "gpt-5.6-sol-high"


def test_codex_attribution_never_guesses_a_missing_field(tmp_path: Path):
    # A build that writes no model must yield None, not a plausible default — the ledger is
    # only worth anything if attribution was genuinely observed.
    rows = [{"timestamp": "2026-07-23T22:09:57.512Z", "type": "session_meta",
             "payload": {"session_id": "s-1"}}]
    got = events.attribution_codex(_write_rows(tmp_path / "r.jsonl", rows))
    assert (got.model, got.agent_version, got.session_id) == (None, None, "s-1")


def test_codex_harness_is_wired_to_its_attribution_reader():
    assert harness.CODEX.attribution is events.attribution_codex


def test_codex_reader_distinguishes_current_pytest_completion_output():
    assert events._codex_is_error("Script completed\n77 passed in 0.79s") is False
    assert events._codex_is_error("Script completed\n1 failed, 76 passed in 1.07s") is True


def test_codex_reads_the_exit_status_the_structured_result_records():
    """Codex frames a structured tool result with its own `Process exited with code N`. It is
    a real exit status — the only one a Codex rollout carries — and it was being ignored in
    favour of guessing the outcome from the runner's prose."""
    passed = "Wall time: 1.1 seconds\nProcess exited with code 0\nOutput:\n77 passed in 1.07s\n"
    failed = "Wall time: 1.1 seconds\nProcess exited with code 1\nOutput:\n1 failed, 76 passed\n"
    assert events._codex_is_error(passed) is False
    assert events._codex_is_error(failed) is True
    # Where it actually buys something: output no runner summary can classify. The prose
    # fallback returns None here — "can't tell" — while the status is right there.
    assert events._codex_is_error("Process exited with code 2\nOutput:\nruff: not found\n") is True
    assert events._codex_is_error("Process exited with code 0\nOutput:\nAll checks passed!\n") is False


def test_codex_exit_status_comes_from_the_header_not_the_command_output():
    # The header precedes `Output:`, so a command that prints this phrase itself — grepping a
    # log, echoing a transcript — must not overwrite the status Codex recorded.
    text = (
        "Process exited with code 0\nOutput:\n"
        "$ grep -r 'exited' build.log\nProcess exited with code 1\n"
    )
    assert events._codex_is_error(text) is False


def test_codex_falls_back_to_the_runner_summary_without_an_exit_status():
    # The freeform shape records no status at all; the prose is all there is.
    assert events._codex_is_error("Script completed\n1 failed, 76 passed in 1.07s") is True


def test_codex_reader_keeps_the_runner_output_not_just_its_verdict():
    # The rollout carries the text; the reader used to distil is_error from it and drop it.
    # is_error is worthless once the shell masks the status — the engine must re-read output.
    evs = events.parse_codex(CODEX_FIXTURE)
    runs = [e for e in evs if e.tool == "Bash" and "pytest" in e.input.get("command", "")]
    assert runs and any("passed" in (e.result.get("stdout") or "") for e in runs)


def test_codex_recovers_a_red_suite_whose_status_the_shell_masked():
    # `; echo done` throws pytest's status away; the recovery is possible only because the
    # reader normalized the output into `result` for the engine to re-read.
    ev = Event(
        ts=100.0,
        tool="Bash",
        input={"command": "pytest -q; echo done"},
        is_error=False,  # echo's status, not pytest's
        result={"stdout": events._codex_output_text(
            [{"type": "input_text", "text": "Script completed\nOutput:\n"},
             {"type": "input_text", "text": "1 failed, 76 passed in 1.07s\n"}]
        )},
    )
    session = Session(
        events=(ev,), edits=(), repo=Path("/repo"), config=Config(),
        git=GitSnapshot(True, "0" * 40, ()),
    )
    assert checks.command_execution(session).status is CheckStatus.FAIL


def test_opencode_reader_extracts_exported_tools():
    evs = events.parse_opencode(OPENCODE_FIXTURE)
    # Names normalize into Tycho's vocabulary (bash->Bash, edit->Edit); the rest of
    # OpenCode's toolset passes through as-is.
    assert {"Bash", "Edit"} <= {e.tool for e in evs}
    pytest_run = next(e for e in evs if "pytest" in e.input.get("command", ""))
    assert pytest_run.is_error is False  # read from state.metadata.exit
    assert [e.path for e in events.file_edits(evs)] == ["/repo/tycho/opencode.py"]


# --- opencode SQLite reader (opencode.db, no `opencode export`) --------------

def _tool_part(tool: str, state: dict) -> dict:
    return {"type": "tool", "tool": tool, "state": state}


def _make_opencode_db(
    path: Path, session_id: str, cwd: Path, parts: list[dict], user_created: int | None = None
) -> None:
    """Build a minimal opencode.db with one assistant message holding `parts`.

    `user_created` prepends a real user message (ms epoch), the way OpenCode opens a
    turn — it carries no tool parts, only the timestamp the boundary anchors on.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, version TEXT, time_updated INT)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INT, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INT, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?,?,?,?)", (session_id, str(cwd), "1.17.20", 2))
    if user_created is not None:
        conn.execute("INSERT INTO message VALUES (?,?,?,?)", ("m0", session_id, user_created, json.dumps(
            # `summary.diffs` is real: OpenCode hangs full patches off a user message.
            {"role": "user", "time": {"created": user_created}, "summary": {"diffs": ["<patch>"]}}
        )))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
        "m1", session_id, (user_created or 0) + 1, json.dumps({"role": "assistant"})
    ))
    for i, part in enumerate(parts):
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (f"pt{i}", "m1", session_id, 10 + i, json.dumps(part)))
    conn.commit()
    conn.close()


_OC_BASH = _tool_part("bash", {
    "status": "completed", "input": {"command": "pytest -q"}, "output": "1 passed",
    "metadata": {"exit": 0}, "time": {"start": 1784054120000, "end": 1784054121000},
})
_OC_EDIT = _tool_part("edit", {
    "status": "completed", "input": {"filePath": "/repo/src/app.py", "oldString": "x=1", "newString": "x=2"},
    "metadata": {}, "time": {"start": 1784054112244, "end": 1784054112246},
})


def test_opencode_session_json_rebuilds_export_shape(tmp_path: Path):
    db = tmp_path / "opencode.db"
    _make_opencode_db(db, "ses_1", tmp_path, [_OC_EDIT, _OC_BASH, {"type": "text", "text": "hi"}])
    data = opencode.session_json("ses_1", tmp_path, db)
    assert data["info"]["directory"] == str(tmp_path)
    # text part dropped; tool parts kept and parseable by the shared reader.
    evs = events.parse_opencode(opencode.materialize("ses_1", tmp_path, db))
    assert [e.tool for e in evs] == ["Edit", "Bash"]
    assert evs[-1].input["command"] == "pytest -q" and evs[-1].is_error is False


def test_opencode_session_json_keeps_user_messages_and_groups_parts(tmp_path: Path):
    """: the materializer no longer flattens the session into one fake message.

    It used to read only the `part` table and synthesize a single `{"role": "assistant"}`
    message, which threw the user rows away — and with them the only turn boundary
    OpenCode has. The DB always carried it; Tycho dropped it before the reader looked.
    """
    db = tmp_path / "opencode.db"
    _make_opencode_db(db, "ses_1", tmp_path, [_OC_EDIT, _OC_BASH], user_created=1784054110000)
    data = opencode.session_json("ses_1", tmp_path, db)

    user, assistant = data["messages"]
    assert [m["info"]["role"] for m in data["messages"]] == ["user", "assistant"]
    assert user["info"]["time"]["created"] == 1784054110000  # the boundary, preserved
    assert user["parts"] == []                     # a user message carries no tool parts
    assert len(assistant["parts"]) == 2            # parts group under their own message
    assert "summary" not in user["info"]           # the diffs blob isn't copied through


def test_opencode_latest_session_by_directory(tmp_path: Path):
    db = tmp_path / "opencode.db"
    _make_opencode_db(db, "ses_1", tmp_path, [_OC_BASH])
    assert opencode.latest_session(tmp_path, db) == "ses_1"
    assert opencode.latest_session(tmp_path / "other", db) is None


def test_opencode_reader_missing_db_is_safe(tmp_path: Path):
    missing = tmp_path / "nope.db"
    assert opencode.latest_session(tmp_path, missing) is None
    assert opencode.session_json("ses_1", tmp_path, missing) is None
    assert opencode.materialize("ses_1", tmp_path, missing) is None


def test_hook_run_opencode_reads_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "opencode.db"
    _make_opencode_db(db, "ses_1", tmp_path, [_OC_EDIT, _OC_BASH])
    monkeypatch.setattr(opencode, "db_path", lambda: db)
    payload = json.dumps({"harness": "opencode", "sessionID": "ses_1", "directory": str(tmp_path)})
    out = hook.run(payload)
    assert out is not None and "Tycho:" in out["message"]


def test_opencode_discover_materializes_latest(tmp_path: Path, monkeypatch):
    db = tmp_path / "opencode.db"
    _make_opencode_db(db, "ses_1", tmp_path, [_OC_BASH])
    monkeypatch.setattr(opencode, "db_path", lambda: db)
    path = harness.OPENCODE.discover(tmp_path)
    assert path is not None and events.parse_opencode(path)[0].tool == "Bash"


def test_claude_reader_returns_nothing_on_cursor_transcript():
    # Confirms Cursor genuinely needs its own reader (no ids/tool_result blocks).
    assert events.parse(CURSOR_FIXTURE) == ()


# --- hook.run end to end ----------------------------------------------------

def test_hook_run_claude_produces_systemMessage(tmp_path: Path):
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(CLAUDE_FIXTURE)})
    out = hook.run(payload)
    assert out is not None and "Tycho:" in out["systemMessage"]


def test_hook_run_cursor_produces_followup_message(tmp_path: Path):
    payload = json.dumps(
        {"workspace_roots": [str(tmp_path)], "transcript_path": str(CURSOR_FIXTURE)}
    )
    out = hook.run(payload)
    assert out is not None and "Tycho:" in out["followup_message"]


def test_hook_run_codex_produces_system_message(tmp_path: Path):
    payload = json.dumps({
        "hook_event_name": "Stop",
        "turn_id": "turn-current",
        "cwd": str(tmp_path),
        "transcript_path": str(CODEX_FIXTURE),
    })
    out = hook.run(payload)
    assert out is not None and "Tycho:" in out["systemMessage"]


def test_hook_run_fails_open_on_bad_input():
    assert hook.run("not json") is None
    assert hook.run(json.dumps([1, 2, 3])) is None  # not a dict
    assert hook.run(json.dumps({"cwd": "/x"})) is None  # no transcript_path


def test_hook_run_fails_open_on_unreadable_transcript(tmp_path: Path):
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(tmp_path / "nope.jsonl")})
    assert hook.run(payload) is None  # never break the agent's Stop


def test_hook_stays_silent_when_turn_has_no_verifiable_activity(tmp_path: Path):
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text(json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}}))
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(transcript)})
    assert hook.run(payload) is None


# --- tycho init -------------------------------------------------------------
# init only touches harnesses it detects, and asks first. Both preconditions are stated
# outright here; detection and consent themselves are tested in test_init_safety.py.


def _init_all(repo: Path, **kw) -> list[str]:
    # Auto-detect surfaces only Claude now (Claude-only usage); the non-Claude installers
    # are kept and still exercised here, so call them by name rather than via detection.
    for name in (".claude", ".cursor", ".codex", ".opencode"):
        (repo / name).mkdir(exist_ok=True)
    return [
        init_mod._install_claude(repo),
        init_mod._install_cursor(repo),
        init_mod._install_codex(repo),
        init_mod._install_opencode(repo),
    ]


def test_init_installs_all_harnesses(tmp_path: Path):
    _init_all(tmp_path)
    claude = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    opencode = (tmp_path / ".opencode" / "plugins" / "tycho.js").read_text()
    assert init_mod._is_tycho_hook(claude["hooks"]["Stop"][0]["hooks"][0]["command"])
    assert init_mod._is_tycho_hook(cursor["hooks"]["stop"][0]["command"])
    assert init_mod._is_tycho_hook(codex["hooks"]["Stop"][0]["hooks"][0]["command"])
    # Codex also gets the SessionStart bootup-notice hook.
    assert init_mod._is_tycho_session_start(codex["hooks"]["SessionStart"][0]["hooks"][0]["command"])
    # OpenCode's plugin drives both the Stop verdict (session.idle) and the bootup notice
    # (session.created → the session-start entrypoint), toasted via the same channel.
    assert "session.idle" in opencode and 'harness: "opencode"' in opencode
    assert "session.created" in opencode and "noticeCommand" in opencode
    assert cursor["version"] == 1


def test_init_is_idempotent(tmp_path: Path):
    _init_all(tmp_path)
    _init_all(tmp_path)  # second run must not duplicate
    claude = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    cursor = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(claude["hooks"]["Stop"]) == 1  # no duplicate group
    assert len(cursor["hooks"]["stop"]) == 1
    assert len(codex["hooks"]["Stop"]) == 1
    assert len(codex["hooks"]["SessionStart"]) == 1  # SessionStart not duplicated either
    plugin = (tmp_path / ".opencode" / "plugins" / "tycho.js").read_text()
    assert plugin.count("session.idle") == 1 and plugin.count("session.created") == 1


def test_init_repairs_a_broken_command(tmp_path: Path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    # a stale bare `tycho hook` that fails without a global install
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "tycho hook"}]}]}}))
    _init_all(tmp_path)
    stop = json.loads(settings.read_text())["hooks"]["Stop"]
    assert len(stop) == 1  # repaired in place, not duplicated
    assert init_mod._is_tycho_hook(stop[0]["hooks"][0]["command"])


# --- runner detection -------------------------------------------------------

def _bash(cmd: str) -> Event:
    return Event(ts=1.0, tool="Bash", input={"command": cmd}, is_error=False)


def test_runner_segment_matches_interpreter_invoked_by_path():
    # Regression: Tycho's own CLAUDE.md documents `.venv/bin/python -m pytest -q`,
    # and every runner check was blind to it — the path prefix defeated the match.
    assert checks._runner_segment(".venv/bin/python -m pytest -q") == "python -m pytest -q"
    assert checks._runner_segment("/usr/local/bin/python3 -m pytest") == "python3 -m pytest"
    assert checks._runner_segment(".venv/bin/pytest -q") == "pytest -q"


def test_command_execution_sees_venv_interpreter_run():
    session = Session(
        events=(_bash(".venv/bin/python -m pytest -q"),),
        edits=(),
        repo=Path("/repo"),
        config=Config(),
        files={},
        git=GitSnapshot(is_repo=False, head_sha=None, changed_paths=()),
    )
    result = checks.command_execution(session)
    assert result.status == CheckStatus.PASS
    assert "python -m pytest -q" in result.evidence


def test_runner_segment_still_ignores_a_quoted_runner_name():
    # The segment split must keep protecting against this after the path fix.
    assert checks._runner_segment('echo "remember to run pytest"') is None


def test_runner_segment_supports_common_ecosystem_runners():
    commands = (
        "python -m unittest", "mvn test", "java -jar junit-platform-console.jar",
        "java org.testng.TestNG suite.xml", "npx mocha", "dotnet test", "ctest",
        "go test ./...", "cargo test", "vendor/bin/phpunit", "bundle exec rspec",
        "gradlew test", "swift test", "flutter test",
    )
    for command in commands:
        assert checks._runner_segment(command) is not None, command


# --- path normalization (git_state / scope_drift correctness) ---------------

def test_relpath_normalizes_absolute_in_repo_paths(tmp_path: Path):
    assert engine._relpath(str(tmp_path / "docs/x.md"), tmp_path) == "docs/x.md"
    assert engine._relpath("src/a.py", tmp_path) == "src/a.py"  # already relative
    assert engine._relpath("/etc/passwd", tmp_path) == "/etc/passwd"  # outside repo, unchanged
    # Always forward slashes, so paths reconcile with git and scope globs. Holds on any
    # host OS — the string form is OS-independent here.
    assert engine._relpath("src\\a.py", tmp_path) == "src/a.py"


def test_relpath_relativizes_posix_absolute_paths_on_any_host():
    # `Path.is_absolute()` is host-flavored: on Windows a drive-less POSIX path reads as
    # relative, so `/repo/old.md` never normalized and the codex fixture failed only there.
    assert engine._relpath("/repo/old.md", Path("/repo")) == "old.md"
    assert engine._relpath("/repo/docs/new.md", Path("/repo")) == "docs/new.md"
    assert engine._relpath("/other/x.md", Path("/repo")) == "/other/x.md"  # outside, unchanged


def test_git_state_counts_uncommitted_with_relative_paths():
    # Regression: absolute edit paths never matched git's relative diff, so the
    # uncommitted count was always 0. With repo-relative paths it's real.
    session = Session(
        events=(),
        edits=(FileEdit(path="docs/x.md", ts=1.0, original=None, kind="edit"),),
        repo=Path("/repo"),
        config=Config(),
        files={"docs/x.md": FileState(path="docs/x.md", exists=True, mtime=1.0, current_text="hi")},
        git=GitSnapshot(is_repo=True, head_sha="abc", changed_paths=("docs/x.md",)),
    )
    result = checks.git_state(session)
    assert result.status == CheckStatus.PASS
    assert "1 uncommitted" in result.evidence


# --- manual discovery -------------------------------------------------------

CWD = Path("/proj/app")


def _home_is(tmp_path: Path, monkeypatch) -> None:
    """Point harness discovery at `tmp_path` through the real `Path.home()` fallback.

    conftest sets `TYCHO_CLAUDE_HOME` for every test, so the suite never reads a developer's
    real `~/.claude` (`init.global_installed()` does). These tests are about the fallback
    *underneath* that override, so they drop it — deleting beats setting, and doing it here
    keeps the reason in one place instead of four.
    """
    monkeypatch.delenv("TYCHO_CLAUDE_HOME", raising=False)
    monkeypatch.setattr(harness.Path, "home", lambda: tmp_path)


def _make_claude(home: Path, mtime: float) -> Path:
    d = home / ".claude" / "projects" / "-proj-app"
    d.mkdir(parents=True)
    p = d / "sess.jsonl"
    p.write_text("{}\n")
    os.utime(p, (mtime, mtime))
    return p


def _make_cursor(home: Path, mtime: float) -> Path:
    d = home / ".cursor" / "projects" / "proj-app" / "agent-transcripts" / "x"
    d.mkdir(parents=True)
    p = d / "x.jsonl"
    p.write_text("{}\n")
    os.utime(p, (mtime, mtime))
    return p


def test_discover_skips_disabled_harnesses(tmp_path, monkeypatch):
    # Cursor is newer, but only Claude is enabled in usage now — discovery skips the rest.
    _home_is(tmp_path, monkeypatch)
    _make_claude(tmp_path, mtime=100.0)
    _make_cursor(tmp_path, mtime=200.0)  # newer, but disabled
    path, h = harness.discover(CWD)
    assert h.name == "claude" and path.name == "sess.jsonl"


def test_discover_claude_when_it_is_newer(tmp_path, monkeypatch):
    _home_is(tmp_path, monkeypatch)
    _make_claude(tmp_path, mtime=300.0)  # newer
    _make_cursor(tmp_path, mtime=200.0)
    path, h = harness.discover(CWD)
    assert h.name == "claude" and path.name == "sess.jsonl"


def test_discover_only_filter_forces_harness(tmp_path, monkeypatch):
    _home_is(tmp_path, monkeypatch)
    _make_claude(tmp_path, mtime=100.0)
    _make_cursor(tmp_path, mtime=999.0)  # newer, but filtered out
    path, h = harness.discover(CWD, only="claude")
    assert h.name == "claude"


def test_discover_none_when_nothing_found(tmp_path, monkeypatch):
    _home_is(tmp_path, monkeypatch)
    assert harness.discover(CWD) == (None, None)


def test_encode_maps_windows_separators_and_spaces():
    # Real ~/.claude/projects ground truth: drive colon, backslashes and spaces all collapse
    # to '-'. Pure*Path fixes the string form, so this pins the encoding on every CI host.
    from pathlib import PurePosixPath, PureWindowsPath

    assert (
        harness._encode(PureWindowsPath(r"C:\Users\user\My Projects\tycho"))
        == "C--Users-user-My-Projects-tycho"
    )
    # POSIX form is a strict subset — "/" and "." only — and is unchanged by the fix.
    assert harness._encode(PurePosixPath("/proj/my.app")) == "-proj-my-app"


# --- configurable data roots -------------------------------------


def test_home_defaults_to_dotdir_under_home(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("TYCHO_CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert harness.home("claude") == tmp_path / ".claude"


def test_home_honors_harness_native_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("TYCHO_CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert harness.home("codex") == tmp_path / "elsewhere"


def test_home_tycho_override_beats_native_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native"))
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", str(tmp_path / "tycho"))
    assert harness.home("claude") == tmp_path / "tycho"


def test_home_expands_user_in_override(tmp_path, monkeypatch):
    # expanduser() reads the home env var itself, so set it rather than Path.home.
    # POSIX reads $HOME; Windows reads %USERPROFILE% — set both so the test is portable.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TYCHO_CURSOR_HOME", "~/relocated")
    assert harness.home("cursor") == tmp_path / "relocated"


def test_home_ignores_empty_override(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", "")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert harness.home("claude") == tmp_path / ".claude"


def test_discover_claude_from_overridden_root(tmp_path, monkeypatch):
    # $HOME holds nothing; the transcript only exists under the override.
    monkeypatch.setattr(harness.Path, "home", lambda: tmp_path / "empty-home")
    root = tmp_path / "relocated" / ".claude"
    (root / "projects" / "-proj-app").mkdir(parents=True)
    (root / "projects" / "-proj-app" / "sess.jsonl").write_text("{}\n")
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", str(root))
    path, h = harness.discover(CWD, only="claude")
    assert h is not None and h.name == "claude" and path.name == "sess.jsonl"


def test_discover_codex_from_overridden_root(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.Path, "home", lambda: tmp_path / "empty-home")
    sessions = tmp_path / "relocated" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(json.dumps({"payload": {"cwd": str(CWD)}}) + "\n")
    monkeypatch.setenv("TYCHO_CODEX_HOME", str(tmp_path / "relocated"))
    # Codex is disabled in the usage-facing `discover`, so exercise its own (kept) adapter
    # directly — this still proves TYCHO_CODEX_HOME resolves the relocated sessions root.
    path = harness.CODEX.discover(CWD)
    assert path is not None and path.name == "s.jsonl"


def test_opencode_db_path_honors_tycho_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TYCHO_OPENCODE_HOME", str(tmp_path / "relocated"))
    assert opencode.db_path() == tmp_path / "relocated" / "opencode.db"


def test_opencode_db_path_falls_back_to_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("TYCHO_OPENCODE_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert opencode.db_path() == tmp_path / "xdg" / "opencode" / "opencode.db"


def test_init_preserves_existing_config(tmp_path: Path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [{"x": 1}]}}))
    _init_all(tmp_path)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"  # untouched
    # Tycho appends its own PreToolUse group; the user's unrecognized entry survives verbatim,
    # `hooks` key absent as they wrote it.
    assert data["hooks"]["PreToolUse"][0] == {"x": 1}
    assert init_mod._is_tycho_owned(data["hooks"]["PreToolUse"][-1]["hooks"][0]["command"])
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][0]["hooks"][0]["command"])


def test_pre_tool_use_reroutes_a_masked_runner(capsys, monkeypatch):
    from tycho.wire import hook as hook_mod

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "permission_mode": "bypassPermissions",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q 2>&1 | tail -5", "description": "run tests"},
    })))
    assert hook_mod.pre_tool_use() == 0
    out = json.loads(capsys.readouterr().out)
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["command"].startswith("tycho exec -- pytest -q") or " exec -- pytest -q" in updated["command"]
    # Everything else the harness sent back untouched: dropping a field would silently
    # discard part of the call the agent made.
    assert updated["description"] == "run tests"


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},   # nothing masks it
    {"tool_name": "Read", "tool_input": {"file_path": "x"}},         # not a shell tool
    {"tool_name": "Bash", "tool_input": {"command": 42}},            # not even a string
    {"tool_name": "Bash"},                                           # no input at all
    {},
])
def test_pre_tool_use_says_nothing_rather_than_guessing(capsys, monkeypatch, payload):
    """Silence leaves the command exactly as the agent wrote it. This hook is the only one
    that can change what runs, so every uncertain path has to end in no output."""
    from tycho.wire import hook as hook_mod

    # Bypass mode, so each case is silent for its *own* reason and not because the gate below
    # turned the rewrite off — otherwise this parametrization would pass with the body removed.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "permission_mode": "bypassPermissions"})))
    assert hook_mod.pre_tool_use() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", None])
def test_pre_tool_use_leaves_the_command_alone_where_permission_rules_apply(capsys, monkeypatch, tmp_path, mode):
    """Claude Code matches `Bash(...)` rules against the *rewritten* command (2.1.220), so
    rewriting under any mode that consults them voids the user's own allowlist: a command that
    ran silently starts asking, and headless it is denied outright."""
    from tycho.wire import hook as hook_mod

    payload = {"tool_name": "Bash", "cwd": str(tmp_path),
               "tool_input": {"command": "pytest -q 2>&1 | tail -5"}}
    if mode is not None:
        payload["permission_mode"] = mode
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook_mod.pre_tool_use() == 0
    assert capsys.readouterr().out == ""


def test_pre_tool_use_rewrites_under_permission_rules_once_opted_in(capsys, monkeypatch, tmp_path):
    """The escape hatch has to open as well as shut, or `tycho rewrite --on` is decoration."""
    from tycho.store import state
    from tycho.wire import hook as hook_mod

    state.set_rewrite_enabled(tmp_path, enabled=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "tool_name": "Bash", "permission_mode": "default", "cwd": str(tmp_path),
        "tool_input": {"command": "pytest -q 2>&1 | tail -5"},
    })))
    assert hook_mod.pre_tool_use() == 0
    out = json.loads(capsys.readouterr().out)
    assert " exec -- pytest -q" in out["hookSpecificOutput"]["updatedInput"]["command"]


def test_pre_tool_use_survives_junk_on_stdin(capsys, monkeypatch):
    from tycho.wire import hook as hook_mod

    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert hook_mod.pre_tool_use() == 0
    assert capsys.readouterr().out == ""

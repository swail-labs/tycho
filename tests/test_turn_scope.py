""": the Stop reports the turn under review, not the whole session.

The fixture is a two-turn session: turn 1 writes app.py, turn 2 only reads it. Every
test here turns on that asymmetry — turn 2 must not be credited with turn 1's work.
"""

import json
import os
from pathlib import Path

from tycho import checks, events, harness, hook
from tycho import verify as engine
from tycho.config import Config
from tycho.model import CheckStatus, Event, FileEdit, FileState, GitSnapshot, Session

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_multiturn.jsonl"
SINGLE_TURN = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"
OPENCODE = Path(__file__).parent / "fixtures" / "opencode_transcript_sample.json"


def make_session(events_=(), edits=(), turn_start=0.0, files=None) -> Session:
    return Session(
        events=tuple(events_),
        edits=tuple(edits),
        repo=Path("/repo"),
        config=Config(),
        files=files or {},
        git=GitSnapshot(False, None, ()),
        turn_start=turn_start,
    )


# --- the boundary -----------------------------------------------------------

def test_turn_start_is_the_last_user_message():
    # The fixture's second user message, 2026-07-13T14:25:00Z, and not the first.
    assert events.turn_start(FIXTURE) == events._epoch("2026-07-13T14:25:00.000Z")


def test_turn_start_ignores_meta_user_entries():
    assert not events._is_user_prose(
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "noise"}}
    )


def test_turn_start_ignores_tool_result_carriers():
    """Claude delivers tool results as user-role messages; they don't open a turn."""
    assert not events._is_user_prose(
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}}
    )


def test_turn_start_counts_a_text_block_user_message():
    """A prompt sent with a pasted image arrives as blocks, not a bare string."""
    assert events._is_user_prose(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    )


def test_turn_start_of_single_turn_transcript_is_zero():
    """No user message => the whole transcript is the turn. Never narrow on a guess."""
    assert events.turn_start(SINGLE_TURN) == 0.0


# --- the relay boundary: each re-check scopes to its own prose ----------------
#
# The relay re-invokes the assistant with no new user message, so several iterations share
# one user turn. A stop_hook_summary opens the next iteration the way a user message opens
# the first — else a re-check re-fails prose an earlier one already answered.

_RELAY_ENTRY = {
    "type": "system",
    "subtype": "stop_hook_summary",
    "hookAdditionalContext": [
        "🔍 Tycho: FAILED\n[TYCHO] The above is an automated verification of the "
        "turn you just finished — a report. Automatic re-check 1 of 3."
    ],
}


def test_is_relay_boundary_recognizes_a_tycho_stop_summary():
    assert events._is_relay_boundary({**_RELAY_ENTRY, "timestamp": "2026-07-13T14:26:00.000Z"})


def test_is_relay_boundary_rejects_non_relay_entries():
    assert not events._is_relay_boundary(
        {"type": "system", "subtype": "stop_hook_summary", "hookAdditionalContext": ["unrelated"]}
    )
    assert not events._is_relay_boundary(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "In Review → Done"}]}}
    )


def test_turn_start_anchors_on_a_later_relay_boundary(tmp_path):
    """A relay re-check after the last user message wins: turn_start moves to the
    boundary so the iteration's prose is judged alone (not the previous one's)."""
    t = tmp_path / "relay.jsonl"
    t.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Patch it"},
                    "timestamp": "2026-07-13T14:25:00.000Z"}) + "\n"
        + json.dumps({**_RELAY_ENTRY, "timestamp": "2026-07-13T14:26:00.000Z"}) + "\n",
        encoding="utf-8",
    )
    assert events.turn_start(t) == events._epoch("2026-07-13T14:26:00.000Z")


# --- opencode's boundary -----------------------------------------
#
# Three real consecutive turns from a captured opencode.db. Same asymmetry as above, on the
# harness that used to be session-scoped because the materializer dropped the user messages
# marking these boundaries.

def test_turn_start_opencode_is_the_last_user_message():
    # Turn C's user message. In seconds, not the ms OpenCode stores — Event.ts is
    # seconds, and a ms boundary would sit ~1000x in the future and blank out the turn.
    assert events.turn_start_opencode(OPENCODE) == 1784055998.578


def test_turn_start_opencode_is_zero_without_a_user_message(tmp_path: Path):
    """The honest fallback survives: an assistant-only session is one whole turn."""
    lone = tmp_path / "session.json"
    lone.write_text('{"info": {}, "messages": [{"info": {"role": "assistant"}, "parts": []}]}')
    assert events.turn_start_opencode(lone) == 0.0


def test_opencode_turn_scoping_narrows_edits_but_not_the_session(tmp_path: Path):
    session = engine.gather(
        OPENCODE, tmp_path, parse=events.parse_opencode, turn_start=events.turn_start_opencode
    )
    # Session scope still sees turn A's edit; the turn under review is the two greps.
    assert [fe.path for fe in session.edits] == ["/repo/tycho/opencode.py"]
    assert session.turn_edits == ()
    assert len(session.turn_events) == 2 and len(session.events) == 30


def test_opencode_stop_says_nothing_on_a_turn_that_edited_nothing():
    """ acceptance, through the wired harness: turn C is credited with nothing.

    This is the exact bug — before the fix the boundary was 0.0, so turn C's Stop
    reported turn A's edit as its own work.
    """
    assert harness.OPENCODE.turn_start(OPENCODE) == 1784055998.578  # wired, not 0.0
    session = engine.gather(
        OPENCODE, Path("/repo"),
        parse=harness.OPENCODE.parse, turn_start=harness.OPENCODE.turn_start,
    )
    assert not checks.has_verifiable_activity(session)


def test_opencode_turn_scoping_does_not_silence_real_work():
    """The mirror: turn A's own Stop must still report turn A's edit."""
    session = engine.gather(
        OPENCODE, Path("/repo"), parse=events.parse_opencode, turn_start=lambda _: 0.0
    )
    assert checks.has_verifiable_activity(session)


# --- the scoped views -------------------------------------------------------

def test_turn_edits_excludes_earlier_turns():
    old = FileEdit(path="app.py", ts=100.0, original=None, kind="create")
    new = FileEdit(path="lib.py", ts=300.0, original=None, kind="create")
    session = make_session(edits=[old, new], turn_start=200.0)
    assert session.edits == (old, new)  # session scope is preserved, not replaced
    assert session.turn_edits == (new,)


def test_turn_start_zero_keeps_every_edit():
    edit = FileEdit(path="app.py", ts=0.0, original=None, kind="create")
    assert make_session(edits=[edit]).turn_edits == (edit,)


# --- acceptance: a turn that edited nothing ---------------------------------

def test_no_verifiable_activity_on_a_read_only_turn():
    """The ticket's core case: earlier edits must not make a read-only turn 'active'."""
    old = FileEdit(path="app.py", ts=100.0, original=None, kind="create")
    assert not checks.has_verifiable_activity(make_session(edits=[old], turn_start=200.0))


def test_file_state_unsupported_on_a_read_only_turn():
    old = FileEdit(path="app.py", ts=100.0, original=None, kind="create")
    result = checks.file_state(make_session(edits=[old], turn_start=200.0))
    assert result.status is CheckStatus.UNSUPPORTED
    assert "this turn" in result.evidence


def test_evidence_says_session_when_nothing_narrowed_the_view():
    """`tycho verify` audits a whole session — calling that "this turn" is the same lie."""
    old = FileEdit(path="app.py", ts=100.0, original=None, kind="create")
    on_disk = {"app.py": FileState(path="app.py", exists=True, mtime=100.0, current_text="x = 1\n")}
    evidence = checks.file_state(make_session(edits=[old], turn_start=0.0, files=on_disk)).evidence
    assert "this session" in evidence and "turn" not in evidence


def test_command_execution_unsupported_when_the_runner_ran_last_turn():
    ran = Event(ts=100.0, tool="Bash", input={"command": "pytest -q"}, is_error=False)
    result = checks.command_execution(make_session(events_=[ran], turn_start=200.0))
    assert result.status is CheckStatus.UNSUPPORTED


def test_gather_scopes_the_read_only_turn(tmp_path: Path):
    """Through the real gather(): the fixture's only edit belongs to turn 1."""
    session = engine.gather(FIXTURE, tmp_path, turn_start=events.turn_start)
    assert [fe.path for fe in session.edits] == ["app.py"]  # session still sees it
    assert session.turn_edits == ()  # the turn under review does not
    assert not checks.has_verifiable_activity(session)


def test_hook_says_nothing_on_a_read_only_turn(tmp_path: Path):
    """End-to-end acceptance, straight through the real Stop entrypoint."""
    payload = f'{{"transcript_path": "{FIXTURE}", "cwd": "{tmp_path}"}}'
    assert hook.run(payload) is None


def test_turn_scoping_does_not_silence_real_work(tmp_path: Path):
    """The mirror of the above — turn 1's Stop must still report turn 1's write."""
    session = engine.gather(FIXTURE, tmp_path, turn_start=lambda _: 0.0)
    assert checks.has_verifiable_activity(session)


# --- session-scoped checks stay session-scoped ------------------------------

def _freshness_session(tmp_path: Path, edit_ts: float, turn_start: float) -> Session:
    src = tmp_path / "app.py"
    src.write_text("x = 1\n")
    os.utime(src, (300.0, 300.0))  # on disk later than the green run at ts=200
    return Session(
        events=(Event(ts=200.0, tool="Bash", input={"command": "pytest -q"}, is_error=False),),
        edits=(FileEdit(path="app.py", ts=edit_ts, original=None, kind="edit"),),
        repo=tmp_path,
        config=Config(),
        files={"app.py": engine._file_state(tmp_path, "app.py")},
        git=GitSnapshot(False, None, ()),
        turn_start=turn_start,
    )


def test_freshness_still_sees_edits_from_earlier_turns(tmp_path: Path):
    """A source edited three turns ago and never retested genuinely is stale."""
    result = checks.test_freshness(_freshness_session(tmp_path, edit_ts=100.0, turn_start=1000.0))
    assert result.status is CheckStatus.STALE
    # ...but the wording must not imply this turn touched it, or a doc-only turn reads
    # as an accusation. It says the file was last edited in an earlier turn instead.
    assert "earlier turn" in result.evidence
    assert "s after the last passing test run" not in result.evidence


def test_freshness_of_a_this_turn_edit_keeps_the_attributing_wording(tmp_path: Path):
    """When the stale file *was* edited this turn, the 'edited Ns after' phrasing is honest."""
    result = checks.test_freshness(_freshness_session(tmp_path, edit_ts=100.0, turn_start=50.0))
    assert result.status is CheckStatus.STALE
    assert "s after the last passing test run" in result.evidence
    assert "earlier turn" not in result.evidence

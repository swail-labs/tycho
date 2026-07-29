"""The durable per-turn record (strategy §9.2): schema, ladder, redaction, retention, reads.

The record is the substrate four other surfaces read (turn digest, `blame`/`log`, the
attestation trailer, the decay ledger), so these tests pin the *shape* and the *contract*
as much as the behaviour — a field rename here breaks all four.
"""

import json
import os
from pathlib import Path

import pytest

from tycho.read import events, harness
from tycho.wire import hook
from tycho.store import record, state
from tycho.read import session as engine
from tycho.store.config import Config
from tycho.model import (
    Attribution,
    CheckResult,
    CheckStatus,
    Event,
    FileEdit,
    FileState,
    GitSnapshot,
    Message,
    Session,
    Stage,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures"
ATTRIBUTION = FIXTURES / "transcript_attribution.jsonl"
CLAUDE_FIXTURE = FIXTURES / "transcript_sample.jsonl"


def make_session(events_=(), edits=(), files=None, messages=(), attribution=None,
                 turn_start=0.0) -> Session:
    return Session(
        events=tuple(events_),
        edits=tuple(edits),
        repo=Path("/repo"),
        config=Config(),
        files=files or {},
        git=GitSnapshot(False, None, ()),
        messages=tuple(messages),
        attribution=attribution or Attribution("claude-opus-5", "2.1.220", "sess-1"),
        turn_start=turn_start,
    )


def bash(command, ts=100.0, is_error=False, result=None) -> Event:
    return Event(ts=ts, tool="Bash", input={"command": command}, is_error=is_error,
                 result=result or {})


def on_disk(path: str, ts=100.0, kind="edit"):
    """One edit plus the FileState proving it actually landed."""
    return (FileEdit(path=path, ts=ts, original=None, kind=kind),
            {path: FileState(path=path, exists=True, mtime=ts, current_text="x = 1\n")})


def passing(name="command_execution") -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "ran without error")


def build(session, results=(), verdict=Verdict.VERIFIED, ended_at=200.0) -> dict:
    return record.build(session, list(results), verdict, "claude", ended_at)


# --- schema shape and stability ---------------------------------------------

_FIELDS = {
    "schema": int, "id": str, "session": str, "harness": str, "model": str,
    "agent_version": str, "started_at": float, "ended_at": float, "verdict": str,
    "stage": str, "checks": list, "files": list, "commands": list, "claims": list,
}


def test_record_carries_exactly_the_documented_fields():
    edit, files = on_disk("app.py")
    rec = build(make_session([bash("pytest -q")], [edit], files, [Message(99.0, "done")]),
                [passing()])
    assert set(rec) == set(_FIELDS)
    for name, kind in _FIELDS.items():
        assert isinstance(rec[name], kind), name


def test_schema_is_the_first_key_on_disk(tmp_path: Path):
    """A migration must be able to read the version without parsing the rest."""
    record.append(tmp_path, build(make_session()))
    line = record.path_for(tmp_path).read_text().splitlines()[0]
    assert line.startswith('{"schema":1,')


def test_verdict_and_stage_are_plain_strings():
    rec = build(make_session(), [passing()], Verdict.FAILED)
    assert rec["verdict"] == "FAILED"
    assert rec["stage"] == "claim_supported"
    json.dumps(rec)  # must survive a round trip with no custom encoder


def test_checks_files_and_commands_have_stable_sub_shapes():
    edit, files = on_disk("src/app.py", kind="create")
    rec = build(make_session([bash("pytest -q")], [edit], files), [passing()])
    assert rec["checks"] == [
        {"name": "command_execution", "status": "PASS", "evidence": "ran without error"}
    ]
    assert rec["files"] == [{"path": "src/app.py", "kind": "create", "ts": 100.0}]
    assert rec["commands"] == [{"cmd": "pytest -q", "runner": True, "outcome": "passed"}]


def test_turn_id_is_stable_and_content_derived():
    session = make_session()
    assert build(session)["id"] == build(session)["id"]
    assert build(session, ended_at=201.0)["id"] != build(session, ended_at=200.0)["id"]


def test_build_is_pure_no_file_is_written(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    build(make_session([bash("pytest -q")]))
    assert not (tmp_path / ".tycho").exists()


def test_unknown_attribution_is_null_never_guessed():
    rec = build(make_session(attribution=Attribution()))
    assert rec["model"] is None and rec["agent_version"] is None and rec["session"] is None


# --- the acceptance ladder, at every rung -----------------------------------

def test_stage_attempted_prose_only():
    session = make_session(messages=[Message(99.0, "I looked at the code.")])
    assert record.stage_of(session, []) is Stage.ATTEMPTED


def test_stage_executed_when_a_recognized_runner_ran():
    session = make_session([bash("pytest -q")])
    assert record.stage_of(session, []) is Stage.EXECUTED


def test_stage_executed_ignores_an_unrecognized_command():
    session = make_session([bash("ls -la")])
    assert record.stage_of(session, []) is Stage.ATTEMPTED


def test_stage_artifact_changed_when_the_file_is_on_disk():
    edit, files = on_disk("app.py")
    session = make_session([bash("pytest -q")], [edit], files)
    assert record.stage_of(session, []) is Stage.ARTIFACT_CHANGED


def test_stage_is_not_artifact_changed_when_the_file_is_missing():
    """A claimed edit is not a rung — the file being there is."""
    edit = FileEdit(path="app.py", ts=100.0, original=None, kind="create")
    files = {"app.py": FileState("app.py", exists=False, mtime=None, current_text=None)}
    session = make_session([bash("pytest -q")], [edit], files)
    assert record.stage_of(session, []) is Stage.EXECUTED


def test_stage_claim_supported_on_a_substantive_pass():
    assert record.stage_of(make_session(), [passing("command_execution")]) is Stage.CLAIM_SUPPORTED


def test_stage_weak_check_pass_is_not_claim_supported():
    """Same bar verify.verdict_of uses to reach VERIFIED — reused, so it can't drift."""
    edit, files = on_disk("app.py")
    session = make_session(edits=[edit], files=files)
    results = [passing("file_state"), passing("git_state")]
    assert engine.verdict_of(results) is not Verdict.VERIFIED
    assert record.stage_of(session, results) is Stage.ARTIFACT_CHANGED


def test_stage_ladder_is_ordered_highest_first():
    edit, files = on_disk("app.py")
    session = make_session([bash("pytest -q")], [edit], files)
    assert record.stage_of(session, [passing()]) is Stage.CLAIM_SUPPORTED


# --- commands ----------------------------------------------------------------

def test_command_outcome_failed_and_unknown():
    session = make_session([
        bash("pytest -q", ts=1.0, is_error=True),
        bash("pytest -q | tail -1", ts=2.0, is_error=False),  # status masked by the pipe
        bash("ls", ts=3.0, is_error=False),
    ])
    outcomes = [(c["runner"], c["outcome"]) for c in build(session)["commands"]]
    assert outcomes == [(True, "failed"), (True, "unknown"), (False, "passed")]


def test_commands_ignore_non_shell_tools():
    session = make_session([Event(ts=1.0, tool="Read", input={"file_path": "a.py"})])
    assert build(session)["commands"] == []


def test_commands_are_bounded():
    session = make_session([bash(f"echo {i}", ts=float(i)) for i in range(200)])
    assert len(build(session)["commands"]) == record._MAX_COMMANDS


# --- redaction ---------------------------------------------------------------

@pytest.mark.parametrize("label, text, secret", [
    ("env assignment", "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY",
     "wJalrXUtnFEMIK7MDENGbPxRfiCY"),
    ("api key", 'API_KEY="abcd1234efgh5678"', "abcd1234efgh5678"),
    ("token", "GITHUB_TOKEN=ghp_0123456789abcdefghijklmnop", "ghp_0123456789abcdefghijklmnop"),
    ("auth header", 'curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"', "eyJhbGciOiJIUzI1NiJ9"),
    ("long password flag", "mysql --password=hunter2horse -u root", "hunter2horse"),
    ("short password flag", "mysql -pHunter2Horse -u root", "Hunter2Horse"),
    ("url credentials", "git push https://user:s3cr3t@github.com/o/r", "s3cr3t"),
    ("stripe key", "sk-live-0123456789abcdefghij", "0123456789abcdefghij"),
    ("aws key id", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("slack token", "xoxb-123456789012-abcdefghij", "123456789012-abcdefghij"),
    ("hex blob", "key=0123456789abcdef0123456789abcdef0123456789abcdef",
     "0123456789abcdef0123456789abcdef0123456789abcdef"),
    ("base64 blob", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefgh",
     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefgh"),
])
def test_every_redaction_pattern_fires(label, text, secret):
    out = record.redact(text)
    assert "[REDACTED]" in out, label
    assert secret not in out, label  # the value is gone, not merely marked


def test_redaction_keeps_what_identifies_the_removal():
    out = record.redact("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRf")
    assert out == "export AWS_SECRET_ACCESS_KEY=[REDACTED]"


@pytest.mark.parametrize("benign", [
    "mkdir -p /tmp/build && pytest -q",
    "docker run -p 8080:80 app",
    "find . -printf '%p\\n'",
    "git checkout 0123456789abcdef0123456789abcdef01234567",  # a git sha is not a secret
    "pytest -q tests/test_record.py",
])
def test_redaction_leaves_ordinary_commands_alone(benign):
    assert record.redact(benign) == benign


def test_redaction_applies_to_commands_evidence_and_claims():
    session = make_session(
        [bash("deploy --token=abcd1234efgh5678ijkl")],
        messages=[Message(99.0, "I set API_KEY=supersecretvalue123 in the env")],
    )
    rec = build(session, [CheckResult("x", CheckStatus.PASS, "ran `p --token=abcd1234efgh5678ijkl`")])
    blob = json.dumps(rec)
    assert "abcd1234efgh5678ijkl" not in blob
    assert "supersecretvalue123" not in blob
    assert blob.count("[REDACTED]") == 3


def test_truncation_bounds_a_pathological_turn():
    session = make_session(
        [bash("echo " + "x" * 5000)],
        messages=[Message(99.0, "y" * 50_000) for _ in range(50)],
    )
    rec = build(session)
    assert len(rec["commands"][0]["cmd"]) < record._MAX_CMD_CHARS + 32
    assert len(rec["claims"]) == record._MAX_CLAIMS
    assert all(len(c) < record._MAX_CLAIM_CHARS + 32 for c in rec["claims"])
    assert rec["claims"][0].endswith(record._TRUNCATED)


def test_redaction_runs_before_truncation():
    """Truncating first could cut a secret in half and leave its head on disk."""
    secret = "A" * 60 + "b" * 59 + "7"  # a mixed-case base64-ish blob, longer than the bound
    rec = build(make_session(messages=[Message(1.0, "x" * (record._MAX_CLAIM_CHARS - 10) + secret)]))
    assert secret[:40] not in rec["claims"][0]


# --- the attestation digest --------------------------------------------------

def test_digest_is_deterministic_and_prefixed():
    rec = build(make_session([bash("pytest -q")]), [passing()])
    assert record.digest(rec) == record.digest(rec)
    assert record.digest(rec).startswith("sha256:")
    assert len(record.digest(rec)) == len("sha256:") + 64


def test_digest_is_key_order_independent():
    rec = build(make_session([bash("pytest -q")]), [passing()])
    shuffled = dict(reversed(list(rec.items())))
    assert list(shuffled) != list(rec)
    assert record.digest(shuffled) == record.digest(rec)


def test_digest_survives_a_json_round_trip(tmp_path: Path):
    rec = build(make_session([bash("pytest -q")]), [passing()])
    record.append(tmp_path, rec)
    assert record.digest(record.read(tmp_path)[0]) == record.digest(rec)


def test_digest_changes_with_content():
    a = build(make_session(), [passing()], Verdict.VERIFIED)
    b = build(make_session(), [passing()], Verdict.FAILED)
    assert record.digest(a) != record.digest(b)


# --- append / read -----------------------------------------------------------

def test_append_then_read_newest_first(tmp_path: Path):
    for i in range(5):
        assert record.append(tmp_path, build(make_session(), ended_at=float(i)))
    rows = record.read(tmp_path)
    assert [r["ended_at"] for r in rows] == [4.0, 3.0, 2.0, 1.0, 0.0]
    assert [r["ended_at"] for r in record.read(tmp_path, limit=2)] == [4.0, 3.0]


def test_iter_records_is_oldest_first(tmp_path: Path):
    for i in range(3):
        record.append(tmp_path, build(make_session(), ended_at=float(i)))
    assert [r["ended_at"] for r in record.iter_records(tmp_path)] == [0.0, 1.0, 2.0]


def test_reads_stream_and_stay_bounded(tmp_path: Path):
    """`tycho log -n 3` must hold 3 records, whatever the file's length."""
    import types

    assert isinstance(record.iter_records(tmp_path), types.GeneratorType)  # streamed, never slurped
    for i in range(50):
        record.append(tmp_path, build(make_session(), ended_at=float(i)))
    rows = record.read(tmp_path, limit=3)
    assert [r["ended_at"] for r in rows] == [49.0, 48.0, 47.0]


def test_read_of_a_missing_file_is_empty(tmp_path: Path):
    assert record.read(tmp_path) == []
    assert list(record.iter_records(tmp_path)) == []
    assert record.touching(tmp_path, "app.py") == []


def test_read_with_a_nonpositive_limit_is_empty(tmp_path: Path):
    record.append(tmp_path, build(make_session()))
    assert record.read(tmp_path, limit=0) == []
    assert record.touching(tmp_path, "app.py", limit=0) == []


def test_corrupt_lines_are_skipped_not_fatal(tmp_path: Path):
    record.append(tmp_path, build(make_session(), ended_at=1.0))
    path = record.path_for(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write("[1, 2, 3]\n")          # valid JSON, wrong shape
        # A truncated append, written the way a crash writes one: **no trailing newline**.
        # With one, this is a different bug — and the shape a killed process never produces.
        fh.write('{"schema":1,"id":"x"')
    record.append(tmp_path, build(make_session(), ended_at=2.0))
    assert [r["ended_at"] for r in record.read(tmp_path)] == [2.0, 1.0]


def test_appends_do_not_interleave(tmp_path: Path):
    """Concurrent-ish appends: every line must stay one whole record."""
    import threading

    def worker(n):
        record.append(tmp_path, build(make_session(messages=[Message(1.0, "c" * 500)]),
                                      ended_at=float(n)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = list(record.iter_records(tmp_path))
    assert len(rows) == 20
    assert sorted(r["ended_at"] for r in rows) == [float(i) for i in range(20)]


# --- blame ------------------------------------------------------------------

def test_touching_finds_records_for_a_path_newest_first(tmp_path: Path):
    edit_a, files_a = on_disk("src/app.py")
    edit_b, files_b = on_disk("src/other.py")
    record.append(tmp_path, build(make_session(edits=[edit_a], files=files_a), ended_at=1.0))
    record.append(tmp_path, build(make_session(edits=[edit_b], files=files_b), ended_at=2.0))
    record.append(tmp_path, build(make_session(edits=[edit_a], files=files_a), ended_at=3.0))
    hits = record.touching(tmp_path, "src/app.py")
    assert [h["ended_at"] for h in hits] == [3.0, 1.0]
    assert record.touching(tmp_path, "src/app.py", limit=1)[0]["ended_at"] == 3.0


def test_touching_accepts_a_bare_basename_and_a_dotslash_path(tmp_path: Path):
    edit, files = on_disk("src/app.py")
    record.append(tmp_path, build(make_session(edits=[edit], files=files)))
    assert record.touching(tmp_path, "app.py")
    assert record.touching(tmp_path, "./src/app.py")
    assert record.touching(tmp_path, "pp.py") == []  # a suffix is not a path segment


# --- retention ---------------------------------------------------------------

def test_max_records_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("TYCHO_TURNS_MAX", raising=False)
    assert record.max_records() == 5000
    monkeypatch.setenv("TYCHO_TURNS_MAX", "12")
    assert record.max_records() == 12
    monkeypatch.setenv("TYCHO_TURNS_MAX", "0")
    assert record.max_records() == 1  # never "keep nothing"
    monkeypatch.setenv("TYCHO_TURNS_MAX", "junk")
    assert record.max_records() == 5000  # read inside the Stop hook — never raise


def test_file_is_pruned_to_the_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TYCHO_TURNS_MAX", "10")
    monkeypatch.setattr(record, "_PRUNE_SLACK", 2)
    for i in range(40):
        record.append(tmp_path, build(make_session(), ended_at=float(i)))
    rows = record.read(tmp_path)
    assert len(rows) <= 12
    assert rows[0]["ended_at"] == 39.0  # the newest always survives
    assert rows[-1]["ended_at"] > 20.0  # the oldest are what got dropped


def test_prune_only_fires_past_the_slack(tmp_path: Path, monkeypatch):
    """The common append is one write, never a rewrite."""
    monkeypatch.setenv("TYCHO_TURNS_MAX", "5")
    monkeypatch.setattr(record, "_PRUNE_SLACK", 100)
    for i in range(20):
        record.append(tmp_path, build(make_session(), ended_at=float(i)))
    assert len(record.read(tmp_path)) == 20


# --- the never-raises contract ----------------------------------------------

def test_append_of_unserializable_content_fails_quietly(tmp_path: Path):
    assert record.append(tmp_path, {"schema": 1, "bad": object()}) is False


def test_append_to_an_unwritable_location_fails_quietly(tmp_path: Path):
    blocker = tmp_path / ".tycho"
    blocker.write_text("i am a file, not a directory")
    assert record.append(tmp_path, build(make_session())) is False


def test_read_of_a_directory_is_quiet(tmp_path: Path):
    record.path_for(tmp_path).mkdir(parents=True)
    assert record.read(tmp_path) == []


def test_redact_tolerates_empty_and_non_string():
    assert record.redact("") == ""
    assert record._clean(None, 10) == ""


def test_build_survives_a_garbage_session():
    """Feed it a session with junk in every optional slot; it must still produce a record."""
    session = make_session(
        [Event(ts=0.0, tool="Bash", input={}, is_error=None)],
        [FileEdit(path="", ts=0.0, original=None, kind="")],
        messages=[Message(0.0, "")],
    )
    rec = build(session, [], Verdict.INDETERMINATE)
    assert rec["schema"] == record.SCHEMA and rec["stage"] == "attempted"


# --- model attribution ------------------------------------------------------

def test_attribution_reads_a_real_claude_transcript_shape():
    """Pinned to the shape verified against ~/.claude/projects (assistant `message.model`,
    top-level `version`/`sessionId`); the fixture keeps this test off that real file."""
    attr = events.attribution(ATTRIBUTION)
    assert attr.model == "claude-opus-5"       # the LAST assistant row wins
    assert attr.agent_version == "2.1.220"
    assert attr.session_id == "0de21c42-25ad-4bd4-92ea-cb9b4e9302bc"


def test_attribution_of_a_transcript_without_the_fields_is_all_none():
    assert events.attribution(CLAUDE_FIXTURE) == Attribution()


def test_attribution_of_an_unreadable_transcript_is_all_none(tmp_path: Path):
    assert events.attribution(tmp_path / "nope.jsonl") == Attribution()


def test_claude_harness_supplies_attribution():
    assert harness.CLAUDE.attribution is events.attribution


def test_a_harness_without_a_reader_supplies_nothing():
    assert harness.CURSOR.attribution(ATTRIBUTION) == Attribution()


def test_gather_threads_attribution_onto_the_session(tmp_path: Path):
    session = engine.gather(ATTRIBUTION, tmp_path, attribution=events.attribution)
    assert session.attribution.model == "claude-opus-5"


def test_gather_without_a_reader_leaves_attribution_empty(tmp_path: Path):
    assert engine.gather(ATTRIBUTION, tmp_path).attribution == Attribution()


# --- the hook wiring --------------------------------------------------------

def _stop(repo: Path, transcript: Path) -> str:
    return json.dumps({"cwd": str(repo), "transcript_path": str(transcript)})


def test_hook_writes_one_record_per_verified_turn(tmp_path: Path):
    assert hook.run(_stop(tmp_path, CLAUDE_FIXTURE)) is not None
    rows = record.read(tmp_path)
    assert len(rows) == 1
    assert rows[0]["harness"] == "claude" and rows[0]["schema"] == record.SCHEMA
    assert rows[0]["verdict"] in {v.name for v in Verdict}
    assert rows[0]["ended_at"] >= rows[0]["started_at"]


def test_hook_records_model_attribution(tmp_path: Path):
    hook.run(_stop(tmp_path, ATTRIBUTION))
    rows = record.read(tmp_path)
    assert rows and rows[0]["model"] == "claude-opus-5"
    assert rows[0]["agent_version"] == "2.1.220"
    assert rows[0]["session"] == "0de21c42-25ad-4bd4-92ea-cb9b4e9302bc"


def test_hook_writes_no_record_when_there_is_nothing_to_verify(tmp_path: Path):
    """The honesty rule: the file must never claim a turn was verified when it wasn't."""
    quiet = tmp_path / "quiet.jsonl"
    quiet.write_text(
        json.dumps({"type": "assistant", "timestamp": "2026-07-27T10:00:00.000Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}})
        + "\n",
        encoding="utf-8",
    )
    assert hook.run(_stop(tmp_path, quiet)) is None
    assert record.read(tmp_path) == []


def test_hook_writes_no_record_on_an_unreadable_transcript(tmp_path: Path):
    assert hook.run(_stop(tmp_path, tmp_path / "nope.jsonl")) is None
    assert record.read(tmp_path) == []


def test_hook_still_reports_when_the_record_cannot_be_written(tmp_path: Path):
    """A record we can't write is simply not written — it never costs the user the verdict."""
    (tmp_path / ".tycho").write_text("i am a file, not a directory")  # every write here fails
    out = hook.run(_stop(tmp_path, CLAUDE_FIXTURE))
    assert out is not None and "Tycho:" in out["systemMessage"]


def test_record_lands_beside_the_rest_of_tycho_state(tmp_path: Path):
    assert record.path_for(tmp_path) == state.dir_for(tmp_path) / "turns.jsonl"
    assert record.path_for(tmp_path).name == record.FILE
    assert os.path.dirname(record.path_for(tmp_path)).endswith(".tycho")


def test_a_record_from_a_newer_tycho_is_skipped_not_misread(tmp_path: Path):
    """The file outlives the version that wrote it.

    A `schema: 2` line may have repurposed a key this version thinks it understands, so
    reading it as schema 1 would be a confident wrong answer — the one thing every reader
    here is built to avoid. Skipping is the honest handling.
    """
    path = record.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mine = build(make_session(), ended_at=1.0)
    path.write_text(
        json.dumps(mine) + "\n"
        + json.dumps({**mine, "schema": 99, "verdict": "VERIFIED"}) + "\n"
        + json.dumps({k: v for k, v in mine.items() if k != "schema"}) + "\n",
        encoding="utf-8",
    )
    rows = list(record.iter_records(tmp_path))
    assert len(rows) == 1 and rows[0]["schema"] == record.SCHEMA


# --- redaction: the bound on the work it does --------------------------------

# `ids` is not cosmetic: pytest puts the test id in `PYTEST_CURRENT_TEST`, and Windows caps an
# environment variable at 32767 characters — a generated id from these inputs errors the test.
@pytest.mark.parametrize("hostile", [
    "TOKEN" * 30_000,                       # the `_SECRET_NAME` row's quadratic shape
    "http://" + "a-" * 15_000,              # the URL-credential row's
    "cat log: " + "A" * 150_000,            # a plain `cat` of a large file
    "sk-live-" + "a_" * 15_000,
], ids=["secret-name", "url-credential", "large-cat", "sk-live"])
def test_redaction_of_a_hostile_input_is_bounded_in_wall_time(hostile):
    """Command strings are agent-controlled, `_clean` used to redact *before* truncating, and
    a hang is not an exception — the Stop hook's fail-open never sees one. Measured through
    `hook.run` before the fix: 32k chars → 4.9s, 128k → 74.7s.

    The bound is wall time on purpose: it fails for any future pattern that reintroduces a
    superlinear one, not just for the two that were found.
    """
    import time as _time

    start = _time.perf_counter()
    out = record._clean(hostile, record._MAX_CMD_CHARS)
    elapsed = _time.perf_counter() - start
    assert elapsed < 0.25, f"redaction took {elapsed:.2f}s"
    assert len(out) <= record._MAX_CMD_CHARS + len(record._TRUNCATED)


def test_a_huge_hostile_claim_does_not_hang_the_record(tmp_path: Path):
    """Through `build`, which is what the Stop hook calls. (`checks._runner_segment` and
    `checks._status_is_masked` scan the *unbounded* command string before this and cost ~0.4s
    each at 100k — outside this module, but the same class of problem.)"""
    import time as _time

    session = make_session(messages=[Message(1.0, "SECRET" * 20_000)])
    start = _time.perf_counter()
    rec = build(session)
    elapsed = _time.perf_counter() - start
    assert elapsed < 0.5, f"build took {elapsed:.2f}s"
    assert record.append(tmp_path, rec)


# --- redaction: the corpus ---------------------------------------------------

CAUGHT = [
    ("stripe webhook secret", "STRIPE=whsec_0123456789abcdefghijklmn", "whsec_0123456789abcdefghijklmn"),
    ("huggingface token", "export HF=hf_abcdefghijklmnopqrstuvwxyz012345", "hf_abcdefghijklmnopqrstuvwxyz012345"),
    ("db pass, short name", "DB_PASS=hunter2horse", "hunter2horse"),
    ("app key", "APP_KEY=base64keymaterial123", "base64keymaterial123"),
    ("encryption key", "ENCRYPTION_KEY=abc123xyz789", "abc123xyz789"),
    ("signing key", "SIGNING_KEY=abc123xyz789", "abc123xyz789"),
    ("master key", "MASTER_KEY=abc123xyz789", "abc123xyz789"),
    ("session key", "SESSION_KEY=abc123xyz789", "abc123xyz789"),
    ("bare key", "KEY=abc123xyz789", "abc123xyz789"),
    ("auth", "AUTH=abc123xyz789", "abc123xyz789"),
    ("salt", "SALT=abc123xyz789", "abc123xyz789"),
    ("dsn", "SENTRY_DSN=https://abc123@o1.ingest.sentry.io/2", "abc123"),
    ("docker login spaced -p", "docker login -u bob -p Sup3rSecretPw registry.io", "Sup3rSecretPw"),
    ("ssh-keygen -N", "ssh-keygen -t ed25519 -N Sup3rSecretPw -f id_ed25519", "Sup3rSecretPw"),
    ("curl -u user:pass", "curl -u admin:Sup3rSecretPw https://api.example.com", "Sup3rSecretPw"),
    ("datadog app key after =", "DD_APP=0123456789abcdef0123456789abcdef01234567",
     "0123456789abcdef0123456789abcdef01234567"),
]

PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAx3bV9Qz1kO0pQ7Yd8kZ2mN4tL6vR8sT0uW2xY4zA6bC8dE0fG\n"
    "aBcD1eF2\n"  # the short final line: on its own it matches nothing
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize("label, text, secret", CAUGHT, ids=[c[0] for c in CAUGHT])
def test_the_redaction_corpus_is_caught(label, text, secret):
    out = record.redact(text)
    assert "[REDACTED]" in out, label
    assert secret not in out, label


def test_a_private_key_block_goes_whole_including_its_short_final_line():
    out = record.redact("here is the key:\n" + PEM + "\ndone")
    assert "aBcD1eF2" not in out
    assert "MIIEowIBAAKCAQEA" not in out
    assert "-----BEGIN RSA PRIVATE KEY-----" in out  # what was removed stays identifiable


@pytest.mark.parametrize("benign", [
    "pytest -q",
    "uv run --with pytest pytest -q",
    "git checkout 0123456789abcdef0123456789abcdef01234567",
    "mkdir -p /tmp/build && pytest -q",
    "docker run -p 8080:80 app",
    "find . -printf '%p\\n'",
    "550e8400-e29b-41d4-a716-446655440000",
    "src/tycho/record.py::test_a_very_long_name",
    "/Users/dev/projects/some-repo/src/components/AuthenticationProviderFactory.tsx",
    "class AbstractSingletonProxyFactoryBeanConfigurationDelegate:",
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "Tycho-Attestation: sha256:" + "a" * 64,
])
def test_redaction_leaves_these_byte_for_byte(benign):
    """False positives cost a reader context and, for `sha256:`, destroy Tycho's own
    attestation digests — the evidence the tool exists to produce."""
    assert record.redact(benign) == benign


def test_a_forty_hex_run_is_a_git_sha_only_when_nothing_assigns_it():
    """The exemption was drawn at the wrong level: plenty of real credentials (Datadog
    application keys) are 40 hex, and `=`/`:` in front means it is a value, not an object id."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert record.redact(f"git show {sha}") == f"git show {sha}"
    assert sha not in record.redact(f"DATADOG_APP={sha}")
    assert sha not in record.redact(f'{{"app": "{sha}"}}')


def test_an_attestation_digest_survives_the_record_it_is_computed_over():
    rec = build(make_session([bash("pytest -q")]), [passing()])
    assert record._clean(record.digest(rec), 500) == record.digest(rec)


# --- the never-raises contract, continued ------------------------------------

def test_a_lone_surrogate_in_a_claim_does_not_lose_the_turn(tmp_path: Path):
    """`"\\ud800"` is not encodable UTF-8: the write raised, `append` returned False, and the
    whole turn — every claim and every check in it — was never recorded."""
    rec = build(make_session(messages=[Message(1.0, "I fixed the \ud800 parser")]))
    assert record.append(tmp_path, rec) is True
    rows = record.read(tmp_path)
    assert len(rows) == 1 and rows[0]["claims"]
    assert record.digest(rows[0])  # and a row already on disk still digests


def test_a_huge_retention_cap_does_not_take_the_append_down(tmp_path: Path, monkeypatch):
    """`deque(maxlen=n)` raises OverflowError past sys.maxsize, and OverflowError is not in
    `append`'s except clause: the hook swallowed it and every turn rendered no verdict."""
    monkeypatch.setenv("TYCHO_TURNS_MAX", "99999999999999999999999")
    assert record.max_records() == record._MAX_CEILING
    assert record.append(tmp_path, build(make_session())) is True
    assert len(record.read(tmp_path)) == 1


# --- permissions -------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_record_is_not_world_readable(tmp_path: Path):
    """`turns.jsonl` holds the agent's own prose; 0644 in a shared checkout is an exposure."""
    record.append(tmp_path, build(make_session(messages=[Message(1.0, "private prose")])))
    path = record.path_for(tmp_path)
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_state_json_is_not_world_readable(tmp_path: Path):
    state.record_run(tmp_path, "claude", verdict="FAILED")
    path = state.dir_for(tmp_path) / "last-run.json"
    assert path.stat().st_mode & 0o077 == 0


# --- blame: the suffix match is for a basename -------------------------------

def test_touching_does_not_answer_a_directory_query_with_a_different_file(tmp_path: Path):
    edit, files = on_disk("vendor/src/app.py")
    record.append(tmp_path, build(make_session(edits=[edit], files=files)))
    assert record.touching(tmp_path, "src/app.py") == []      # a different file
    assert record.touching(tmp_path, "app.py")                # a bare basename still matches
    assert record.touching(tmp_path, "vendor/src/app.py")     # and the exact path does

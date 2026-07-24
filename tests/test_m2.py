"""M2 readers: transcript parsing, git/fs readers, and gather()."""

import hashlib
import subprocess
from pathlib import Path

from tycho import events, fsstate, gitstate
from tycho import verify as engine
from tycho.model import Event, FileEdit

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


# --- events -----------------------------------------------------------------

def test_parse_extracts_tool_events_in_time_order():
    evs = events.parse(FIXTURE)
    assert [e.tool for e in evs] == ["Bash", "Bash", "Edit", "Write"]  # text/system ignored, sorted


def test_assistant_messages_extracts_prose_not_tool_blocks():
    msgs = events.assistant_messages(FIXTURE)
    assert len(msgs) == 2  # the two assistant text blocks; tool_use/tool_result ignored
    assert msgs[0].text == "Running lint." and "tests pass" in msgs[1].text
    assert all(m.ts > 0 for m in msgs)  # timestamped like events, for turn scoping


def test_bash_error_signal_and_structured_result():
    evs = events.parse(FIXTURE)
    lint, pytest_ev = evs[0], evs[1]
    assert lint.input["command"] == "ruff check"
    assert lint.is_error is True          # denied Bash → is_error, string toolUseResult → {}
    assert lint.result == {}
    assert pytest_ev.is_error is False
    assert "1 passed" in pytest_ev.result["stdout"]


def test_edit_captures_original_file():
    evs = events.parse(FIXTURE)
    edit = next(e for e in evs if e.tool == "Edit")
    assert edit.result["originalFile"] == "def ok():\n    return False\n"


def test_file_edits_projection():
    edits = events.file_edits(events.parse(FIXTURE))
    by_path = {fe.path: fe for fe in edits}
    assert by_path["src/auth.py"].kind == "edit"
    assert by_path["src/auth.py"].original == "def ok():\n    return False\n"
    assert by_path["tests/test_auth.py"].kind == "create"
    assert by_path["tests/test_auth.py"].original is None


def test_file_edits_drops_denied_edits_and_keeps_the_retry():
    """TYCHO-33: a denied Edit never touched disk — only the successful retry is an edit."""
    # Arrange: a PreToolUse-denied Edit (no toolUseResult), then the same edit succeeding.
    denied = Event(ts=1.0, tool="Edit", input={"file_path": "src/auth.py"}, is_error=True, result={})
    retry = Event(
        ts=2.0,
        tool="Edit",
        input={"file_path": "src/auth.py"},
        is_error=False,
        result={"originalFile": "def ok():\n    return False\n"},
    )

    # Act
    edits = events.file_edits((denied, retry))

    # Assert: one edit, and not the phantom kind=create the denial used to project.
    assert [(fe.ts, fe.kind) for fe in edits] == [(2.0, "edit")]


def test_file_edits_keeps_edits_with_no_recorded_status():
    """is_error=None means no status was recorded, not failure (Cursor records none)."""
    unknown = Event(ts=1.0, tool="Write", input={"path": "notes.md"}, is_error=None, result={})
    assert [fe.path for fe in events.file_edits((unknown,))] == ["notes.md"]


def test_parse_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n{"type":"system"}\n')
    assert events.parse(p) == ()  # no tool_use → no events, and no crash


# --- git / fs readers -------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def test_git_reader_on_a_real_repo(tmp_path: Path):
    _git(tmp_path, "init")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "init")

    assert gitstate.is_repo(tmp_path)
    assert gitstate.head_sha(tmp_path)
    assert gitstate.commit_exists(tmp_path, "HEAD")
    assert not gitstate.commit_exists(tmp_path, "deadbeef")
    assert gitstate.blob_at(tmp_path, "HEAD", "a.py") == "x = 1\n"

    (tmp_path / "a.py").write_text("x = 2\n")
    assert gitstate.diff_names(tmp_path, "HEAD") == ("a.py",)


def test_git_reader_on_non_repo(tmp_path: Path):
    assert not gitstate.is_repo(tmp_path)
    assert gitstate.head_sha(tmp_path) is None
    assert gitstate.diff_names(tmp_path, "HEAD") == ()


def test_with_baseline_recovers_null_original_from_git(tmp_path: Path):
    # TYCHO-32: Claude Code sends originalFile: null on repeat edits. gather() must recover
    # the pre-session baseline from git so the AST tamper checks keep a real "before".
    _git(tmp_path, "init")
    (tmp_path / "test_x.py").write_text("def test():\n    assert x == 1\n")
    _git(tmp_path, "add", "test_x.py")
    _git(tmp_path, "commit", "-m", "init")

    fe = FileEdit(path="test_x.py", ts=1.0, original=None, kind="create")
    out = engine._with_baseline(fe, tmp_path, "HEAD")
    assert out.original == "def test():\n    assert x == 1\n"
    assert out.kind == "edit"  # git has the blob, so it existed — not a create

    # A harness-supplied baseline is never overwritten.
    kept = engine._with_baseline(FileEdit("test_x.py", 1.0, "harness", "edit"), tmp_path, "HEAD")
    assert kept.original == "harness"

    # Untracked path (genuine new file) and out-of-repo path stay unchanged.
    assert engine._with_baseline(FileEdit("new.py", 1.0, None, "create"), tmp_path, "HEAD").original is None
    assert engine._with_baseline(FileEdit("/etc/hosts", 1.0, None, "edit"), tmp_path, "HEAD").original is None


def test_fs_reader(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"hello")
    assert fsstate.exists(f)
    assert fsstate.sha256(f) == hashlib.sha256(b"hello").hexdigest()
    assert isinstance(fsstate.mtime(f), float)

    missing = tmp_path / "nope"
    assert not fsstate.exists(missing)
    assert fsstate.sha256(missing) is None
    assert fsstate.mtime(missing) is None


# --- gather -----------------------------------------------------------------

def test_gather_builds_session(tmp_path: Path):
    session = engine.gather(FIXTURE, tmp_path)
    assert len(session.events) == 4
    assert len(session.edits) == 2
    assert session.repo == tmp_path
    assert session.config.scope_include == ()  # zero-config default


def test_gather_detects_supported_test_layouts_and_ignores_dependencies(tmp_path: Path):
    assert not engine.gather(FIXTURE, tmp_path).has_tests

    dependency = tmp_path / "node_modules" / "pkg"
    dependency.mkdir(parents=True)
    (dependency / "thing.test.js").write_text("")
    assert not engine.gather(FIXTURE, tmp_path).has_tests

    (tmp_path / "widget.spec.ts").write_text("")
    assert engine.gather(FIXTURE, tmp_path).has_tests


def test_test_discovery_covers_supported_ecosystem_conventions(tmp_path: Path):
    names = (
        "test_unit.py", "WidgetTest.java", "widget.test.js", "WidgetTests.cs",
        "widget_test.cpp", "widget_test.go", "widget_test.rs", "WidgetTest.php",
        "widget_spec.rb", "WidgetTest.kt", "WidgetTests.swift", "widget_test.dart",
    )
    for name in names:
        case = tmp_path / name
        case.write_text("")
        assert engine._has_tests(tmp_path), name
        case.unlink()

    (tmp_path / "lib.rs").write_text("#[rstest]\nfn property_case() {}\n")
    assert engine._has_tests(tmp_path)

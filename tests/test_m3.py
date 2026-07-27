"""M3: astdiff + the 8 checks + end-to-end run_checks."""

import subprocess
from dataclasses import replace
from pathlib import Path

from tycho import astdiff, checks
from tycho import verify as engine
from tycho.config import Config
from tycho.model import CheckStatus, Event, FileEdit, FileState, GitSnapshot, Message, Session, Verdict


def make_session(events=(), edits=(), files=None, git=None, config=None, messages=()) -> Session:
    return Session(
        events=tuple(events),
        edits=tuple(edits),
        repo=Path("/repo"),
        config=config or Config(),
        files=files or {},
        git=git or GitSnapshot(False, None, ()),
        messages=tuple(messages),
    )


def bash(command, ts, is_error=False, result=None) -> Event:
    # `result` carries the captured stdout/stderr; empty by default, since three of the
    # four harnesses record none.
    return Event(ts=ts, tool="Bash", input={"command": command}, is_error=is_error, result=result or {})


# --- astdiff ----------------------------------------------------------------

def test_assertion_delta_removed():
    before = "def t():\n    assert a == 1\n    assert b == 2\n"
    after = "def t():\n    assert a == 1\n"
    assert astdiff.assertion_delta(before, after) == ["1 assertion(s) removed"]


def test_assertion_delta_neutralized():
    before = "def t():\n    assert a == 1\n"
    after = "def t():\n    assert True\n"
    assert astdiff.assertion_delta(before, after) == ["1 assertion(s) neutralized to always-true"]


def test_assertion_delta_clean():
    src = "def t():\n    assert a == 1\n"
    assert astdiff.assertion_delta(src, src) == []


def test_assertion_delta_unparseable_returns_empty():
    assert astdiff.assertion_delta("def t(:\n", "def t():\n    assert x\n") == []


def test_skip_added():
    before = "def t():\n    assert x\n"
    after = "import pytest\n@pytest.mark.skip\ndef t():\n    assert x\n"
    out = astdiff.skip_or_mock_added(before, after)
    assert any("skip added" in f for f in out)


def test_mock_added():
    before = "def t():\n    assert real()\n"
    after = "from unittest.mock import patch\ndef t():\n    with patch('m'):\n        assert real()\n"
    out = astdiff.skip_or_mock_added(before, after)
    assert any("mock/patch" in f for f in out)


# --- command_execution ------------------------------------------------------

def test_command_execution_pass():
    s = make_session(events=[bash("pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_fail():
    s = make_session(events=[bash("pytest -q", 100.0, is_error=True)])
    assert checks.command_execution(s).status == CheckStatus.FAIL


def test_command_execution_unsupported_when_no_runner():
    s = make_session(events=[bash("ls", 100.0)])
    assert checks.command_execution(s).status == CheckStatus.UNSUPPORTED


def test_command_execution_unsupported_without_exit_status():
    s = make_session(events=[bash("pytest -q", 100.0, is_error=None)])
    assert checks.command_execution(s).status == CheckStatus.UNSUPPORTED


def test_verifiable_activity_requires_edits_or_a_runner():
    assert not checks.has_verifiable_activity(make_session(events=[bash("ls", 1.0)]))
    assert checks.has_verifiable_activity(make_session(events=[bash("pytest -q", 1.0)]))
    assert checks.has_verifiable_activity(
        make_session(edits=[FileEdit("notes.md", 1.0, None, "create")])
    )


def test_command_execution_ignores_runner_name_inside_echo():
    # a runner name quoted inside an echo/grep must NOT count as the tests running
    s = make_session(events=[bash('echo "run pytest to check" && grep -r pytest .', 100.0)])
    assert checks.command_execution(s).status == CheckStatus.UNSUPPORTED


def test_command_execution_matches_python_m_pytest():
    s = make_session(events=[bash("python -m pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_matches_virtualenv_pytest():
    s = make_session(events=[bash(".venv/bin/pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_matches_windows_venv_python_exe():
    # Windows venv interpreter carries a `.exe` suffix; must still count as a runner
    s = make_session(events=[bash("./.venv/Scripts/python.exe -m pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_masked_by_pipe_is_unsupported():
    # `pytest | tail` exits with tail's status (no pipefail), so is_error=False is a
    # phantom green. Must degrade to UNSUPPORTED, never PASS. No output was
    # captured here, so there is nothing to fall back to and it stays UNSUPPORTED.
    s = make_session(events=[bash("pytest -q 2>&1 | tail -20", 100.0, is_error=False)])
    r = checks.command_execution(s)
    assert r.status == CheckStatus.UNSUPPORTED and "masked by the shell" in r.evidence


def test_command_execution_masked_by_semicolon_is_not_a_green(  # TYCHO-61
):
    # `pytest; echo done` exits with echo's status. Trusting it reported VERIFIED on a red
    # suite — the TYCHO-31 bug on a shape TYCHO-31 didn't cover.
    s = make_session(events=[bash("pytest -q; echo done", 100.0, is_error=False)])
    assert checks.command_execution(s).status != CheckStatus.PASS


def test_command_execution_masked_by_or_true_is_not_a_green():
    # `pytest || true` swallows the failure by construction.
    s = make_session(events=[bash("pytest -q || true", 100.0, is_error=False)])
    assert checks.command_execution(s).status != CheckStatus.PASS


def test_command_execution_trusts_and_chained_runner():
    # `&&` is the safe shape and must NOT be flagged: a red pytest fails the whole
    # command, so a recorded success really is the runner's.
    s = make_session(events=[bash("pytest -q && echo ok", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_recovers_a_red_suite_from_masked_output():
    # The exit code is gone, but the runner said so itself — read it back.
    s = make_session(
        events=[
            bash(
                "pytest -q; echo done",
                100.0,
                is_error=False,
                result={"stdout": "1 failed, 76 passed in 1.07s\ndone", "stderr": ""},
            )
        ]
    )
    r = checks.command_execution(s)
    assert r.status == CheckStatus.FAIL and "read from its output" in r.evidence


def test_command_execution_runner_last_in_pipe_still_scored():
    # runner as the LAST stage owns the exit status — `cat log | pytest -` is honest
    s = make_session(events=[bash("cat args.txt | pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def _msg(text, ts=100.0):
    return Message(ts=ts, text=text)


def _tool(name, ts=100.0):
    return Event(ts=ts, tool=name, input={}, is_error=False)


# --- tool_call_provenance ----------------------------------------

def test_provenance_web_claim_backed_by_search_passes():
    s = make_session(messages=[_msg("I searched the web for the API docs.")], events=[_tool("WebSearch")])
    assert checks.tool_call_provenance(s).status == CheckStatus.PASS


def test_provenance_web_claim_with_no_search_fails():
    s = make_session(messages=[_msg("I searched the web for the API docs.")], events=[_tool("Bash")])
    r = checks.tool_call_provenance(s)
    assert r.status == CheckStatus.FAIL and "web search/fetch" in r.evidence


def test_provenance_issue_claim_backed_by_jira_tool_passes():
    s = make_session(
        messages=[_msg("I created TYCHO-91 and moved TYCHO-29 to In Progress.")],
        events=[_tool("mcp__atlassian__createJiraIssue")],
    )
    assert checks.tool_call_provenance(s).status == CheckStatus.PASS


def test_provenance_issue_claim_with_no_tool_fails():
    s = make_session(messages=[_msg("I filed TYCHO-91 for that.")], events=[_tool("Bash")])
    assert checks.tool_call_provenance(s).status == CheckStatus.FAIL


def test_provenance_unsupported_without_prose():
    # non-Claude harness: no messages captured -> UNSUPPORTED, never a false FAIL
    s = make_session(events=[_tool("Bash")])
    assert checks.tool_call_provenance(s).status == CheckStatus.UNSUPPORTED


def test_provenance_unsupported_when_no_claim_recognized():
    s = make_session(messages=[_msg("I refactored the parser; it reads cleaner now.")], events=[_tool("Bash")])
    assert checks.tool_call_provenance(s).status == CheckStatus.UNSUPPORTED


def test_provenance_does_not_false_fail_on_ambiguous_prose():
    # ticket-shaped verbs without a ticket key, code-sense "resolved", codebase "searched" —
    # none is a tool-action claim, so none may FAIL (never-false-FAIL invariant, TYCHO-91)
    for text in (
        "I moved the helper into utils.py.",
        "I resolved the merge conflict.",
        "I searched the codebase with grep.",
        "This change fixes TYCHO-45.",
        "I'll create a ticket for that later.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("Bash")])
        assert checks.tool_call_provenance(s).status != CheckStatus.FAIL, text


def test_provenance_does_not_false_fail_on_reported_third_party_action():
    # The live false FAIL: the agent *narrates* an action taken by someone else, or reports a
    # ticket's pre-existing state — not a claim it acted this turn. None may FAIL, even with no
    # matching tool call (follow-up to TYCHO-91/95).
    for text in (
        "Dan already closed TYCHO-97, so nothing to do.",
        "Dan Mano moved TYCHO-29 to Done last week.",
        "TYCHO-30 was already closed before we started.",
        "The operator filed TYCHO-50 for that.",
        "Dan searched the web for the changelog earlier.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("Bash")])
        assert checks.tool_call_provenance(s).status != CheckStatus.FAIL, text


def test_provenance_agent_claim_still_fails_beside_a_name():
    # The guard must not swallow a real first-person claim just because a name is nearby: the
    # agent's own subject-dropped/first-person claim still FAILs without a tool call.
    for text in (
        "I closed TYCHO-30 for Dan.",
        "Filed TYCHO-91; TYCHO-30 closed.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("Bash")])
        assert checks.tool_call_provenance(s).status == CheckStatus.FAIL, text


def test_provenance_issue_status_arrow_and_key_first_order_pass():
    # TYCHO-95: the exact live miss — a status-arrow report with the KEY earlier, and the
    # reversed "KEY <verb>" word order — both were invisible before the recall widening.
    for text in (
        "Round-trip complete on TYCHO-92: Hold → In Review, then In Review → Hold.",
        "TYCHO-29 moved to In Progress; TYCHO-30 closed.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("mcp__atlassian__transitionJiraIssue")])
        assert checks.tool_call_provenance(s).status == CheckStatus.PASS, text


def test_provenance_issue_status_arrow_no_tool_fails():
    # a claimed transition ("Hold → In Review" next to a KEY) with no Jira call is fabricated
    s = make_session(
        messages=[_msg("Moved it: TYCHO-92 Hold → In Review.")], events=[_tool("Bash")],
    )
    assert checks.tool_call_provenance(s).status == CheckStatus.FAIL


def test_provenance_observed_status_arrow_does_not_false_fail():
    # The live false FAIL: the agent *observes* where a ticket already sits (a status arrow read
    # off the board), makes no Jira call, and is FAILed for a transition it never performed. An
    # observed arrow is not a self-made one — none may FAIL (follow-up to TYCHO-95).
    for text in (
        "I looked and TYCHO-30 already sits at In Review → Done; I didn't touch it.",
        "TYCHO-30 is now at Hold → Done on the board.",
        "The board shows TYCHO-41 In Review → Done.",
        "It's still at In Progress → Done.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("Bash")])
        assert checks.tool_call_provenance(s).status != CheckStatus.FAIL, text


def test_provenance_future_status_change_does_not_false_fail():
    # TYCHO-95: widening must not catch future/hypothetical — "I'll move" is base-tense and
    # "to Done" is not a two-status arrow, so no claim is recognized (never a false FAIL).
    for text in (
        "I'll move TYCHO-40 to Done tomorrow.",
        "We should transition TYCHO-41 to In Review at some point.",
    ):
        s = make_session(messages=[_msg(text)], events=[_tool("Bash")])
        assert checks.tool_call_provenance(s).status != CheckStatus.FAIL, text


def test_provenance_claim_flips_has_verifiable_activity():
    # an MCP-only turn (a claim, no edits/runners) must make the hook speak
    claim = make_session(messages=[_msg("I created TYCHO-91.")], events=[_tool("Bash")])
    assert checks.has_verifiable_activity(claim)
    quiet = make_session(messages=[_msg("Looks good to me.")], events=[_tool("Bash")])
    assert not checks.has_verifiable_activity(quiet)


def test_command_execution_matches_variable_interpreter(  # TYCHO-88
):
    # `"$PY" -m pytest` — the interpreter is a shell variable we can't resolve, but the
    # `-m pytest` module names the runner. Was invisible ("no test ran") before.
    s = make_session(events=[bash('"$PY" -m pytest -q', 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_variable_interpreter_piped_is_not_a_green():  # TYCHO-88
    # detected as a runner now, but the pipe still masks the status — must not go green
    s = make_session(events=[bash('( cd x && "$PY" -m pytest -q | tail -8 )', 100.0, is_error=False)])
    assert checks.command_execution(s).status != CheckStatus.PASS


def test_command_execution_ignores_module_flag_inside_echo():  # TYCHO-88 guard
    # `-m pytest` behind a non-interpreter (echo) must NOT count as the tests running
    s = make_session(events=[bash('echo "-m pytest"', 100.0)])
    assert checks.command_execution(s).status == CheckStatus.UNSUPPORTED


def test_command_execution_matches_wsl_wrapped_runner():  # TYCHO-89
    # a Windows-hosted agent reaches Linux only via `wsl.exe ... -- bash -c '<cmd>'`; the
    # runner is nested in the wrapper's arg and was invisible before.
    s = make_session(events=[bash("wsl.exe -d Ubuntu -- bash -lc 'python3 -m pytest -q'", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_matches_bash_dash_c_runner():  # TYCHO-89
    s = make_session(events=[bash("bash -c 'pytest -q'", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_command_execution_wrapped_runner_piped_is_not_a_green():  # TYCHO-89
    # the wrapper faithfully forwards pytest's status, but the outer pipe then masks it
    s = make_session(events=[bash("bash -c 'pytest -q' | tail -5", 100.0, is_error=False)])
    assert checks.command_execution(s).status != CheckStatus.PASS


def test_command_execution_matches_tycho_run_wrapper():  # TYCHO-90
    # `tycho run -- <cmd>` execs the child and forwards its real exit code; detection peels
    # the wrapper so the runner inside is seen and trusted.
    s = make_session(events=[bash("tycho run -- pytest -q", 100.0, is_error=False)])
    assert checks.command_execution(s).status == CheckStatus.PASS


def test_masked_pipe_run_does_not_anchor_freshness():
    # the phantom green must not become the last-green anchor: a source edited after it
    # should read UNSUPPORTED ("no passing run"), not STALE against a masked run.
    s = make_session(
        events=[bash("pytest -q | tail -5", 100.0, is_error=False)],
        edits=[FileEdit("src/auth.py", ts=110.0, original="x", kind="edit")],
        files={"src/auth.py": FileState("src/auth.py", True, mtime=200.0, current_text="x")},
    )
    assert checks.test_freshness(s).status == CheckStatus.UNSUPPORTED


def test_command_execution_sees_a_powershell_runner():
    # TYCHO-27: a test suite run through a non-Bash shell tool (PowerShell) must be seen,
    # not dropped by a Bash-only filter.
    ev = Event(ts=100.0, tool="PowerShell", input={"command": "uv run pytest -q"}, is_error=False, result={})
    assert checks.command_execution(make_session(events=[ev])).status == CheckStatus.PASS


def test_runner_segment_matches_uv_run_with_flags_between():
    # `uv run --python 3.12 --with pytest python -m pytest -q` — flags sit between the
    # wrapper and the runner, so the plain prefix match misses it.
    assert checks._runner_segment("uv run --python 3.12 --with pytest python -m pytest -q") is not None
    assert checks._runner_segment("uvx --from build pyproject-build") is None  # not a test runner


def test_uv_run_wrapper_does_not_flag_a_non_test_command():
    # `--with pytest` installs pytest but the command is ruff — must NOT count as a test run.
    assert checks._runner_segment("uv run --with pytest ruff check") is None


# --- test_freshness ---------------------------------------------------------

def test_freshness_stale_when_source_edited_after_run():
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("src/auth.py", ts=110.0, original="x", kind="edit")],
        files={"src/auth.py": FileState("src/auth.py", True, mtime=200.0, current_text="x")},
    )
    r = checks.test_freshness(s)
    assert r.status == CheckStatus.STALE and "src/auth.py" in r.evidence


def test_freshness_ignores_prose_edited_after_run():
    """Editing a doc after a green run can't invalidate it — crying STALE there is a
    false alarm on the one check meant to prove the run still covers the code."""
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("notes/design.md", ts=110.0, original="x", kind="edit")],
        files={"notes/design.md": FileState("notes/design.md", True, mtime=200.0, current_text="x")},
    )
    assert checks.test_freshness(s).status == CheckStatus.UNSUPPORTED


def test_freshness_still_stale_when_a_lockfile_changes_after_run():
    """The exclusion must stay narrow: a dependency change really can break tests."""
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("uv.lock", ts=110.0, original="x", kind="edit")],
        files={"uv.lock": FileState("uv.lock", True, mtime=200.0, current_text="x")},
    )
    assert checks.test_freshness(s).status == CheckStatus.STALE


def test_freshness_pass_when_source_older_than_run():
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("src/auth.py", ts=50.0, original="x", kind="edit")],
        files={"src/auth.py": FileState("src/auth.py", True, mtime=50.0, current_text="x")},
    )
    assert checks.test_freshness(s).status == CheckStatus.PASS


def test_freshness_unsupported_without_green_run():
    s = make_session(edits=[FileEdit("src/a.py", 1.0, "x", "edit")])
    assert checks.test_freshness(s).status == CheckStatus.UNSUPPORTED


# --- test_provenance --------------------------------------------------------

def test_provenance_fail_when_test_edited_after_run():
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("tests/test_a.py", ts=150.0, original="def test_a():\n    assert x\n", kind="edit")],
    )
    r = checks.test_provenance(s)
    assert r.status == CheckStatus.FAIL and "tests/test_a.py" in r.evidence


def test_provenance_pass_when_test_edited_before_run():
    s = make_session(
        events=[bash("pytest -q", 100.0)],
        edits=[FileEdit("tests/test_a.py", ts=50.0, original="x", kind="edit")],
    )
    assert checks.test_provenance(s).status == CheckStatus.PASS


def test_provenance_unsupported_without_test_edits():
    s = make_session(events=[bash("pytest -q", 100.0)])
    assert checks.test_provenance(s).status == CheckStatus.UNSUPPORTED


# --- assertion_weakening / skip_mock ----------------------------------------

def test_assertion_weakening_fail():
    before = "def test_a():\n    assert x == 1\n"
    s = make_session(
        edits=[FileEdit("tests/test_a.py", 10.0, original=before, kind="edit")],
        files={"tests/test_a.py": FileState("tests/test_a.py", True, 10.0, "def test_a():\n    assert True\n")},
    )
    assert checks.assertion_weakening(s).status == CheckStatus.FAIL


def test_skip_injection_fail():
    before = "def test_a():\n    assert x\n"
    after = "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert x\n"
    s = make_session(
        edits=[FileEdit("tests/test_a.py", 10.0, original=before, kind="edit")],
        files={"tests/test_a.py": FileState("tests/test_a.py", True, 10.0, after)},
    )
    assert checks.skip_mock_injection(s).status == CheckStatus.FAIL


def test_ast_checks_unsupported_without_test_edits():
    s = make_session(edits=[FileEdit("src/a.py", 1.0, "x", "edit")])
    assert checks.assertion_weakening(s).status == CheckStatus.UNSUPPORTED


def test_ast_check_distinguishes_missing_baseline_from_no_test_edits():
    # TYCHO-32: a test file WAS edited but carries no baseline (harness sent originalFile:
    # null and git couldn't supply it). Must not read as "tests untouched" — distinct evidence.
    s = make_session(edits=[FileEdit("tests/test_a.py", 1.0, original=None, kind="create")])
    r = checks.assertion_weakening(s)
    assert r.status == CheckStatus.UNSUPPORTED
    assert "no pre-session baseline" in r.evidence and "tests/test_a.py" in r.evidence


# --- file_state -------------------------------------------------------------

def test_file_state_fail_when_missing():
    s = make_session(
        edits=[FileEdit("src/a.py", 1.0, None, "create")],
        files={"src/a.py": FileState("src/a.py", False, None, None)},
    )
    r = checks.file_state(s)
    assert r.status == CheckStatus.FAIL and "missing" in r.evidence


def test_file_state_pass():
    s = make_session(
        edits=[FileEdit("src/a.py", 1.0, None, "create")],
        files={"src/a.py": FileState("src/a.py", True, 1.0, "code\n")},
    )
    assert checks.file_state(s).status == CheckStatus.PASS


# --- git_state --------------------------------------------------------------

def test_git_state_unsupported_when_not_repo():
    s = make_session(edits=[FileEdit("a.py", 1.0, None, "create")])
    assert checks.git_state(s).status == CheckStatus.UNSUPPORTED


def test_git_state_pass_when_edit_in_diff():
    s = make_session(
        edits=[FileEdit("a.py", 1.0, "x", "edit")],
        files={"a.py": FileState("a.py", True, 1.0, "x")},
        git=GitSnapshot(True, "abc", ("a.py",)),
    )
    assert checks.git_state(s).status == CheckStatus.PASS


def test_git_state_fail_on_phantom():
    s = make_session(
        edits=[FileEdit("ghost.py", 1.0, None, "create")],
        files={"ghost.py": FileState("ghost.py", False, None, None)},
        git=GitSnapshot(True, "abc", ()),
    )
    assert checks.git_state(s).status == CheckStatus.FAIL


def test_git_state_unsupported_when_only_out_of_repo_edits():
    # TYCHO-45: an out-of-repo edit (kept absolute by _relpath) exists on disk but git
    # never heard of it. Must NOT report "reconciled with git" on file_state's evidence.
    s = make_session(
        edits=[FileEdit("/home/u/.claude/memory/note.md", 1.0, None, "create")],
        files={"/home/u/.claude/memory/note.md": FileState("/home/u/.claude/memory/note.md", True, 1.0, "hi")},
        git=GitSnapshot(True, "abc", ()),
    )
    r = checks.git_state(s)
    assert r.status == CheckStatus.UNSUPPORTED and "outside the repo" in r.evidence


def test_git_state_counts_only_in_repo_paths_on_a_mixed_turn():
    # TYCHO-45: with both in-repo and out-of-repo edits, judge only the in-repo one and
    # surface the outside count rather than folding it into "reconciled".
    s = make_session(
        edits=[
            FileEdit("src/a.py", 1.0, "x", "edit"),
            FileEdit("/etc/hosts", 1.0, None, "edit"),
        ],
        files={
            "src/a.py": FileState("src/a.py", True, 1.0, "x"),
            "/etc/hosts": FileState("/etc/hosts", True, 1.0, "127.0.0.1"),
        },
        git=GitSnapshot(True, "abc", ("src/a.py",)),
    )
    r = checks.git_state(s)
    assert r.status == CheckStatus.PASS
    assert "1 path(s) edited" in r.evidence and "1 uncommitted" in r.evidence
    assert "1 outside the repo" in r.evidence


# --- scope_drift ------------------------------------------------------------

def test_scope_drift_pass():
    s = make_session(
        edits=[FileEdit("src/a.py", 1.0, "x", "edit")],
        config=Config(scope_include=("src/**",)),
    )
    assert checks.scope_drift(s).status == CheckStatus.PASS


def test_scope_drift_fail():
    s = make_session(
        edits=[FileEdit("infra/deploy.sh", 1.0, "x", "edit")],
        config=Config(scope_include=("src/**",)),
    )
    r = checks.scope_drift(s)
    assert r.status == CheckStatus.FAIL and "infra/deploy.sh" in r.evidence


def test_scope_drift_unsupported_without_config_points_at_the_command():
    s = make_session(edits=[FileEdit("src/a.py", 1.0, "x", "edit")])
    r = checks.scope_drift(s)
    assert r.status == CheckStatus.UNSUPPORTED
    assert "tycho scope add" in r.evidence  # actionable, not a dead end


# --- end-to-end -------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def test_run_checks_end_to_end(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def ok():\n    return True\n")
    (tmp_path / "tests" / "test_auth.py").write_text("def test_ok():\n    assert ok()\n")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    session = engine.gather(FIXTURE, tmp_path)
    results = engine.run_checks(session)
    assert {r.name for r in results} == {c.__name__ for c in checks.CHECKS}
    assert engine.verdict_of(results) in set(Verdict)


def test_run_checks_omits_test_checks_when_repo_has_no_tests():
    session = make_session()
    session = replace(session, has_tests=False)
    # tool_call_provenance is not a test check, so it still runs with no test suite.
    assert {r.name for r in engine.run_checks(session)} == {
        "file_state", "git_state", "scope_drift", "tool_call_provenance"
    }

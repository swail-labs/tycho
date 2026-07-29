"""`verifier_integrity` — the check that reports edits to Tycho itself.

The threat it closes is the one the strategy calls the thesis-breaking hole: the subject of
the audit editing the auditor. So the tests are shaped as attacks, plus the false positives
that would make it unusable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tycho.engine import checks, verdict_of
from tycho.engine.checks.integrity import verifier_integrity
from tycho.model import CheckResult, CheckStatus, Event, FileEdit, Session, Verdict
from tycho.store.config import Config


def session(events=(), edits=(), config=None) -> Session:
    return Session(
        events=tuple(events), edits=tuple(edits), repo=Path("/repo"), config=config or Config()
    )


def bash(command: str) -> Event:
    return Event(ts=1.0, tool="Bash", input={"command": command})


def edit(path: str) -> FileEdit:
    return FileEdit(path=path, ts=1.0, original="x", kind="edit")


# --- the attacks -------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ".tycho.toml",
    ".tycho/turns.jsonl",
    ".tycho/catches.json",
    "nested/.tycho/turns.jsonl",
    ".claude/settings.json",
    # `tycho init --global` writes this one outside the repo; it still makes Tycho run.
    "/Users/dev/.claude/settings.json",
    ".claude/settings.local.json",
    ".cursor/hooks.json",
    ".codex/hooks.json",
    ".opencode/plugins/tycho.js",
    ".git/hooks/prepare-commit-msg",
    ".githooks/prepare-commit-msg",
])
def test_editing_tycho_or_its_wiring_fails(path):
    result = verifier_integrity(session(edits=[edit(path)]))
    assert result.status is CheckStatus.FAIL
    assert path in result.evidence


@pytest.mark.parametrize("command", [
    "rm -f .tycho/turns.jsonl",
    "rm -rf .tycho",
    "mv .tycho.toml /tmp/",
    "truncate -s 0 .tycho/turns.jsonl",
    "echo '' > .tycho.toml",
    "cat /dev/null > .tycho/turns.jsonl",
    "sed -i '' 's/include = .*/include = []/' .tycho.toml",
    "tee .tycho.toml < /tmp/blank",
    "sudo rm .tycho/catches.json",
    "env FOO=1 rm .tycho.toml",
    "VAR=1 rm .tycho.toml",
])
def test_shelling_out_at_tycho_fails(command):
    result = verifier_integrity(session(events=[bash(command)]))
    assert result.status is CheckStatus.FAIL, command


def test_a_fail_sinks_the_whole_verdict():
    """The check is worth nothing if a green elsewhere can outvote it."""
    results = [
        CheckResult("command_execution", CheckStatus.PASS, "pytest exited 0"),
        verifier_integrity(session(edits=[edit(".tycho.toml")])),
    ]
    assert verdict_of(results) is Verdict.FAILED


def test_it_cannot_be_disabled_from_the_config_it_polices():
    """`[checks].disable` lives in `.tycho.toml`, which the agent can write. If naming this
    check there switched it off, the hole would close and reopen in one edit."""
    disabled = Config(disabled_checks=("verifier_integrity", "command_execution"))
    names = {r.name for r in checks.run_checks(session(edits=[edit("src/app.py")], config=disabled))}
    assert "verifier_integrity" in names
    assert "command_execution" not in names  # every other check still obeys the config


def test_no_tests_in_the_repo_does_not_silence_it():
    """The test-check family is dropped in a repo with no tests; this one is not a test check."""
    from dataclasses import replace

    bare = replace(session(edits=[edit(".tycho.toml")]), has_tests=False)
    assert any(r.name == "verifier_integrity" and r.status is CheckStatus.FAIL
               for r in checks.run_checks(bare))


# --- the false positives that would make it unusable -------------------------


@pytest.mark.parametrize("command", [
    "cat .tycho/turns.jsonl",
    "grep -r FAILED .tycho/",
    "head -5 .tycho.toml",
    "ls -la .tycho",
    "tycho scope add 'src/**'",
    "tycho relay --off",
    "tycho override test_freshness 'generated file'",
    "tycho log -n 20",
])
def test_reading_tycho_and_driving_its_cli_are_clean(command):
    assert verifier_integrity(session(events=[bash(command)])).status is CheckStatus.PASS


def test_ordinary_work_passes():
    result = verifier_integrity(
        session(events=[bash("pytest -q"), bash("rm -rf build/")], edits=[edit("src/app.py")])
    )
    assert result.status is CheckStatus.PASS


def test_a_mutating_verb_elsewhere_in_the_line_does_not_convict():
    """`cat .tycho/x && rm scratch` has both a protected path and a destructive verb, in
    different commands. Reading the record is not an attack on it."""
    result = verifier_integrity(session(events=[bash("cat .tycho/turns.jsonl && rm scratch.txt")]))
    assert result.status is CheckStatus.PASS


@pytest.mark.parametrize("path", [
    "/private/tmp/scratch/repo/.tycho/turns.jsonl",
    "/Users/dev/some-other-project/.tycho.toml",
    "/tmp/fixture/.git/hooks/prepare-commit-msg",
])
def test_another_trees_tycho_is_not_this_repos_business(path):
    """Out-of-repo state belongs to `scope_drift`, not here. Caught on Tycho's own turn: a
    smoke test made a scratch `.tycho/` under /tmp and the check called it tampering."""
    assert verifier_integrity(session(edits=[edit(path)])).status is CheckStatus.PASS


def test_an_unexpanded_variable_is_not_a_path():
    """`mkdir -p "$SCRATCH/repo/.tycho"` reaches the record as a literal `$SCRATCH/…`. Only
    the shell knows where that pointed; a checker that guesses convicts on a name."""
    result = verifier_integrity(session(events=[bash('mkdir -p "$SCRATCH/repo/.tycho"')]))
    assert result.status is CheckStatus.PASS


def test_the_repo_bound_does_not_let_a_relative_escape_through():
    """The bound is about *other trees*, not about spelling. A path that walks out and back
    is still this repo's file, and `_relpath` normalizes it before a check ever sees it."""
    assert verifier_integrity(session(edits=[edit(".tycho/turns.jsonl")])).status is CheckStatus.FAIL


def test_paths_that_merely_look_like_tycho_are_clean():
    for path in ("tycho.toml", "src/tycho/app.py", "docs/tycho-notes.md", ".tychoish/x"):
        assert verifier_integrity(session(edits=[edit(path)])).status is CheckStatus.PASS, path


def test_a_turn_that_touched_nothing_is_unsupported():
    """No edits and no commands: there was nothing to examine, and saying PASS would claim
    a corroboration that never happened."""
    assert verifier_integrity(session()).status is CheckStatus.UNSUPPORTED


def test_its_pass_does_not_make_a_blind_turn_look_examined():
    """A turn Tycho can say nothing about reduces to UNSUPPORTED. This check passes on almost
    every turn, so counting its PASS as "Tycho spoke" would make that verdict unreachable and
    silently fold the blind rate into INDETERMINATE."""
    results = [
        CheckResult("command_execution", CheckStatus.UNSUPPORTED, "status masked"),
        verifier_integrity(session(events=[bash("pytest -q | tail -1")])),
    ]
    assert results[1].status is CheckStatus.PASS
    assert verdict_of(results) is Verdict.UNSUPPORTED

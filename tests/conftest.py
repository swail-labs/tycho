"""Keep the suite off the machine's real Tycho state and off the network, and print the eval metric.

`state.record_run` bumps a machine-level tally that lives outside any repo, so a
test recording a FAILED verdict would otherwise inflate the developer's own `tycho count`.
Point TYCHO_HOME at a per-test tmp dir for every test, always.

The update check reaches PyPI; opt every test out by default so nothing makes a
real network call. Tests that exercise the check delete the env var and monkeypatch the fetch.

Also home to the three pieces of scaffolding the whole suite needs: a git runner, a repo
with one commit, and a turn record in the shape `record.build` writes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tycho.store import record


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git against `repo` with an identity and signing config of our own.

    `commit.gpgsign=false` is load-bearing: on a machine with global commit signing every
    commit made here would otherwise block on a passphrase prompt or fail outright.
    """
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=check,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, self-contained git repo with one commit — what `review`/`attest` diff against."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "seed.txt").write_text("seed\n")
    git(root, "add", "seed.txt")
    git(root, "commit", "-qm", "seed")
    return root


def turn_record(**overrides) -> dict:
    """One turn record, in the shape `record.build` writes one."""
    rec = {
        "schema": record.SCHEMA,
        "id": "0" * 16,
        "session": "s",
        "harness": "claude",
        "model": "claude-opus-5",
        "agent_version": "2.1.220",
        "started_at": 1.0,
        "ended_at": 2.0,
        "verdict": "VERIFIED",
        "stage": "claim_supported",
        "checks": [],
        "files": [],
        "commands": [],
        "claims": [],
    }
    rec.update(overrides)
    return rec


@pytest.fixture(autouse=True)
def _isolate_tycho_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path_factory.mktemp("tycho-home")))
    monkeypatch.setenv("TYCHO_NO_UPDATE_CHECK", "1")
    # And off the developer's real `~/.claude`: `init.global_installed()` reads it, so a
    # developer who really ran `tycho init --global` would see unrelated tests change
    # behaviour. `harness.home` reads this var first, redirecting detection and installation
    # together; tests exercising the `Path.home()` fallback delete it instead.
    monkeypatch.setenv("TYCHO_CLAUDE_HOME", str(tmp_path_factory.mktemp("claude-home")))


def pytest_terminal_summary(terminalreporter):
    """Print the eval rates after the run.

    Lives here because `pytest_terminal_summary` is only collected from conftest and
    plugins, never from a test module. `summary_lines()` returns nothing when no scenario
    ran, so a filtered run (`-k count`) stays quiet rather than printing a 0/0 rate as if
    it meant something.
    """
    try:
        from test_eval import summary_lines
    except ImportError:  # eval module missing or unimportable — never fail a run over it
        return
    for line in summary_lines():
        terminalreporter.write_line(line)

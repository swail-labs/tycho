"""Keep the suite off the machine's real Tycho state and off the network, and print the eval metric.

`state.record_run` bumps a machine-level tally that lives outside any repo, so a
test recording a FAILED verdict would otherwise inflate the developer's own `tycho count`.
Point TYCHO_HOME at a per-test tmp dir for every test, always.

The update check reaches PyPI; opt every test out by default so nothing makes a
real network call. Tests that exercise the check delete the env var and monkeypatch the fetch.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_tycho_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("TYCHO_HOME", str(tmp_path_factory.mktemp("tycho-home")))
    monkeypatch.setenv("TYCHO_NO_UPDATE_CHECK", "1")
    # And off the developer's real `~/.claude`. `init.global_installed()` reads the
    # user-level Claude config to decide whether a machine-wide install is already
    # covering this repo, so without this a developer who has actually run
    # `tycho init --global` would see unrelated tests change behaviour — the suite would
    # pass or fail depending on the machine it ran on, which is worse than either.
    # `harness.home` reads this var first, so one override redirects detection and
    # installation together. Tests that exercise the `Path.home()` fallback itself delete
    # it (see test_m4.py) — that still works, because deleting beats setting.
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

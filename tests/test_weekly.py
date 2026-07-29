"""The weekly line — the only thing Tycho ever *pushes* at a user whose agent behaves.

Silence-by-default means a well-behaved repo never hears from Tycho at all, so a user
whose agent rarely gets caught has no evidence the verifier is still alive, and no reason
to keep it. This line is the answer, and it has exactly two ways to go wrong.

It can get chatty — at which point it is no longer a digest, it is the noise the silence
invariant exists to prevent, and the first thing a user turns off. Once per week per repo,
counted from when it was last *shown*, not from when the session started.

Or it can overclaim. Everything on this line is read back off `turns.jsonl`; nothing is
inferred, nothing is rounded up, and a week with no turns produces no line rather than a
cheerful zero. A digest that says "3 caught" when the record holds two is worse than no
digest, for the same reason a green badge over a dead hook is.
"""

from __future__ import annotations

import json
from pathlib import Path

from tycho.store import record, state
from tycho.views import weekly
from tycho.wire import hook

from conftest import turn_record

DAY = 86400
NOW = 1_000_000.0


def _write(repo: Path, *records: dict) -> None:
    for rec in records:
        record.append(repo, rec)


def _turn(ended_at: float, verdict: str = "VERIFIED", checks=()) -> dict:
    return turn_record(ended_at=ended_at, started_at=ended_at - 1, verdict=verdict,
                       checks=[{"name": n, "status": s, "evidence": "e"} for n, s in checks])


# --- what the line says -------------------------------------------------------

def test_a_repo_with_no_turns_says_nothing(tmp_path: Path):
    # Nothing on the record is not "0 turns this week" — it's nothing to say.
    assert weekly.line(tmp_path, now=NOW) is None


def test_the_line_counts_this_weeks_turns_and_catches(tmp_path: Path):
    _write(tmp_path,
           _turn(NOW - DAY, "VERIFIED"),
           _turn(NOW - 2 * DAY, "FAILED", [("test_freshness", "FAIL")]),
           _turn(NOW - 3 * DAY, "STALE", [("test_freshness", "STALE")]))

    line = weekly.line(tmp_path, now=NOW)

    assert line is not None
    assert "3 turns" in line
    assert "2 caught" in line
    assert "1 FAILED" in line and "1 STALE" in line


def test_turns_older_than_the_window_are_not_this_week(tmp_path: Path):
    # The whole point of a *weekly* line is that last month's catch doesn't keep being news.
    _write(tmp_path, _turn(NOW - 30 * DAY, "FAILED", [("test_freshness", "FAIL")]))

    assert weekly.line(tmp_path, now=NOW) is None


def test_a_week_that_caught_nothing_still_reports_its_turns(tmp_path: Path):
    """The quiet week is the one the digest exists for: turns and no catches is the
    evidence that Tycho ran, which is exactly what an invisible tool cannot otherwise show."""
    _write(tmp_path, *[_turn(NOW - DAY, "VERIFIED") for _ in range(4)])

    line = weekly.line(tmp_path, now=NOW)

    assert line is not None
    assert "4 turns" in line
    assert "nothing caught" in line


def test_the_top_offender_is_the_check_that_caught_most(tmp_path: Path):
    _write(tmp_path,
           _turn(NOW - DAY, "FAILED", [("command_execution", "FAIL")]),
           _turn(NOW - DAY, "FAILED", [("test_freshness", "FAIL")]),
           _turn(NOW - DAY, "STALE", [("test_freshness", "STALE")]))

    line = weekly.line(tmp_path, now=NOW)

    assert "top offender: test_freshness" in line


def test_a_check_that_only_passed_is_never_the_offender(tmp_path: Path):
    _write(tmp_path,
           _turn(NOW - DAY, "VERIFIED", [("scope_drift", "PASS")]),
           _turn(NOW - 2 * DAY, "VERIFIED", [("scope_drift", "PASS")]),
           _turn(NOW - 3 * DAY, "FAILED", [("test_freshness", "FAIL")]))

    assert "top offender: test_freshness" in weekly.line(tmp_path, now=NOW)


def test_no_offender_is_named_when_nothing_was_caught(tmp_path: Path):
    _write(tmp_path, _turn(NOW - DAY, "VERIFIED", [("scope_drift", "PASS")]))

    assert "top offender" not in weekly.line(tmp_path, now=NOW)


def test_a_corrupt_record_costs_a_row_not_the_line(tmp_path: Path):
    _write(tmp_path, _turn(NOW - DAY, "FAILED", [("test_freshness", "FAIL")]))
    with record.path_for(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    assert "1 turn" in weekly.line(tmp_path, now=NOW)


# --- how often it speaks ------------------------------------------------------

def test_the_first_week_is_due(tmp_path: Path):
    assert state.weekly_due(tmp_path, now=NOW) is True


def test_a_digest_shown_today_is_not_due_again(tmp_path: Path):
    state.mark_weekly_shown(tmp_path, now=NOW)

    assert state.weekly_due(tmp_path, now=NOW + DAY) is False
    assert state.weekly_due(tmp_path, now=NOW + 6 * DAY) is False


def test_it_comes_due_again_a_week_later(tmp_path: Path):
    state.mark_weekly_shown(tmp_path, now=NOW)

    assert state.weekly_due(tmp_path, now=NOW + 7 * DAY) is True


# --- the hook path ------------------------------------------------------------

class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def _session_start(repo: Path, monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"cwd": str(repo)})))
    hook.session_start()
    return capsys.readouterr().out


def test_session_start_carries_the_digest_to_the_human_channel(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(weekly, "line", lambda repo, now=None: "Tycho this week: 1 turn, 1 caught")

    out = _session_start(tmp_path, monkeypatch, capsys)

    assert "Tycho this week: 1 turn, 1 caught" in json.loads(out)["systemMessage"]


def test_the_second_session_this_week_is_silent(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(weekly, "line", lambda repo, now=None: "Tycho this week: 1 turn, 1 caught")

    first = _session_start(tmp_path, monkeypatch, capsys)
    second = _session_start(tmp_path, monkeypatch, capsys)

    assert "this week" in first
    assert second.strip() == ""


def test_a_week_with_nothing_to_say_does_not_burn_the_slot(tmp_path: Path, monkeypatch, capsys):
    """A silent week must not mark the digest as shown — otherwise the first real catch
    waits up to another seven days for a slot that was spent saying nothing."""
    monkeypatch.setattr(weekly, "line", lambda repo, now=None: None)
    _session_start(tmp_path, monkeypatch, capsys)

    assert state.weekly_due(tmp_path, now=NOW) is True


def test_a_broken_digest_never_takes_the_hook_down(tmp_path: Path, monkeypatch, capsys):
    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(weekly, "line", _boom)

    assert _session_start(tmp_path, monkeypatch, capsys).strip() == ""

"""The eval runner's own gate: which findings fail a run, and which are only reported.

`scripts/harness_eval.py` decides whether CI goes red. If that decision drifts, everything
downstream of it quietly stops meaning anything — a scorecard that never fails is a scorecard
nobody has to satisfy. So the split between "absolute promise" and "reported forever" is
pinned here rather than left to a docstring.

`build()` itself is not exercised: it runs the whole conformance suite in-process, which from
inside that suite would be recursive. What it produces is a dict, and these hold the pure
functions that consume one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import harness_eval  # noqa: E402


def _report(**overrides) -> dict:
    """One clean harness row — every invariant satisfied — plus whatever a test breaks."""
    row = {
        "enabled": True,
        "conformance_passed": 27,
        "conformance_failed": [],
        "reach": 7,
        "reach_of": 8,
        "blind": 1,
        "catch": 1,
        "catch_of": 1,
        "corpus": "captured",
        "version_captured": "2.1.220",
        "version_pinned": "2.1.220",
        "version_installed": "2.1.220",
    }
    row.update(overrides)
    return {"harnesses": {"claude": row}, "fabricated_greens": [], "global_failures": []}


def test_a_clean_report_passes():
    assert harness_eval.invariant_failures(_report(), {}) == []


def test_a_fabricated_green_fails_the_run():
    """The one promise that is not scopeable. A harness Tycho is blind on must decline; if
    being blind ever let it claim VERIFIED over a red session, that is the product lying."""
    report = _report()
    report["fabricated_greens"] = ["codex: red suite, status masked by a pipe"]
    assert harness_eval.invariant_failures(report, {}) != []


def test_a_conformance_failure_fails_the_run():
    problems = harness_eval.invariant_failures(_report(conformance_failed=["test_detect"]), {})
    assert any("conformance" in p for p in problems)


def test_an_enabled_harness_on_an_authored_corpus_fails_the_run():
    """Authored fixtures only prove what their author believed, so they cannot back a harness
    users depend on."""
    problems = harness_eval.invariant_failures(_report(corpus="authored"), {})
    assert any("authored" in p for p in problems)


def test_an_unenabled_harness_on_an_authored_corpus_is_only_reported():
    """Cursor and OpenCode sit here today. Failing on them would mean the eval could not go
    green until every harness was finished, which is how a gate ends up switched off."""
    report = _report(enabled=False, corpus="authored")
    assert harness_eval.invariant_failures(report, {}) == []


def test_a_catch_rate_below_its_floor_fails_the_run():
    assert harness_eval.invariant_failures(_report(catch=0), {"claude": 1}) != []


def test_a_catch_rate_above_its_floor_passes():
    """Floors only ratchet up, and only by a deliberate `--update` commit."""
    assert harness_eval.invariant_failures(_report(catch=3), {"claude": 1}) == []


def test_low_reach_never_fails_the_run():
    """The load-bearing distinction in this file. A harness recording almost nothing is a fact
    about that harness — Cursor keeps no tool_result at all — and no work on Tycho moves it.
    Failing on it would punish the honest declaration and create pressure to overstate what a
    harness records, which is the one thing the declaration exists to prevent.
    """
    report = _report(reach=1, blind=7, catch=0)
    assert harness_eval.invariant_failures(report, {}) == []


def test_reach_stays_on_the_scorecard_even_at_its_worst():
    """Reported forever: hiding it is a quieter version of the lie the eval exists to catch."""
    rendered = "\n".join(harness_eval.render(_report(reach=1, blind=7), {}))
    assert "structurally blind" in rendered
    assert "fabricated greens: 0" in rendered  # printed at zero, so it stays visible


@pytest.mark.parametrize(
    "pinned, installed, expected",
    [
        ("2.1.220", "2.1.220", "ok"),
        ("2.1.220", "2.1.221", "STALE"),
        ("1.17.20", None, "no probe"),  # OpenCode ships no version to ask for
    ],
)
def test_pin_state_reports_drift(pinned: str, installed: str | None, expected: str):
    row = _report(version_pinned=pinned, version_installed=installed)["harnesses"]["claude"]
    assert expected in harness_eval._pin_state(row)

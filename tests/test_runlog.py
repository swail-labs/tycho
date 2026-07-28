"""`runlog` against output real runners actually printed.

The fixtures are captured, not composed (see `fixtures/runner_output.py`). A summary pattern
written from a manual is a claim nobody checked, and four of these entries broke a pattern
that read correct on paper.
"""

import pytest

from fixtures.runner_output import FAILING, NOT_A_VERDICT, PASSING
from tycho.engine import runlog


@pytest.mark.parametrize("runner", sorted(PASSING))
def test_a_green_run_reads_as_green(runner):
    assert runlog.outcome(PASSING[runner]) is False


@pytest.mark.parametrize("runner", sorted(FAILING))
def test_a_red_run_reads_as_red(runner):
    assert runlog.outcome(FAILING[runner]) is True


@pytest.mark.parametrize("case", sorted(NOT_A_VERDICT))
def test_what_is_not_a_summary_yields_no_verdict(case):
    """None, never False. A toolchain that could not build has no verdict to report, and an
    agent's prose about a suite is not the suite's own words."""
    assert runlog.outcome(NOT_A_VERDICT[case]) is None


def test_every_captured_runner_is_read_both_ways():
    """A runner Tycho recognizes only when green is worse than one it doesn't know: it would
    supply passes and stay silent on failures."""
    assert set(FAILING) <= set(PASSING)


def test_a_skipped_test_does_not_make_a_green_run_red():
    """`cargo` reports `ok. 1 passed; 0 failed; 1 ignored` for a suite with a silenced test.
    Reading that as failure would cry wolf on every legitimately-ignored test; the silencing
    is `skip_mock_injection`'s job, and it is caught there."""
    assert runlog.outcome(PASSING["cargo_with_ignored"]) is False
    assert runlog.outcome(PASSING["minitest_with_skip"]) is False
    assert runlog.outcome(PASSING["dart_with_skip"]) is False

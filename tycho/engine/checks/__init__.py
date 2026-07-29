"""The deterministic checks. Each is a pure function of the Session snapshot returning one
CheckResult. A check that doesn't apply returns UNSUPPORTED with a reason — never a false FAIL.
`run_checks` drives the registry and honours `config.disabled_checks`.

One module per check family, over two shared readers:

    cmdread   what command is this, and how much of the suite did it run?
    outcome   what did it return?

`tycho.checks` stays the single import for all of it. The re-exports at the bottom are that
promise kept: callers and tests name `checks.X`, and where X lives is this package's business.
"""

from __future__ import annotations

from ...model import CheckResult, CheckStatus as CheckStatus, Session
from .execution import command_execution
from .freshness import test_freshness, test_provenance
from .integrity import verifier_integrity
from .prose import tool_call_provenance
from .repo import file_state, git_state, scope_drift
from .tamper import assertion_weakening, skip_mock_injection


CHECKS = (
    command_execution,
    tool_call_provenance,
    test_freshness,
    test_provenance,
    assertion_weakening,
    skip_mock_injection,
    file_state,
    git_state,
    scope_drift,
    verifier_integrity,
)


# Checks `[checks].disable` may not switch off. A verifier its own subject can turn off is
# not a verifier, and `.tycho.toml` is a file the agent can write — so the one check that
# reports edits to `.tycho.toml` must not be reachable from inside it.
_UNDISABLEABLE = frozenset({verifier_integrity.__name__})


_TEST_CHECKS = frozenset({
    "command_execution", "test_freshness", "test_provenance",
    "assertion_weakening", "skip_mock_injection",
})


def run_checks(session: Session) -> list[CheckResult]:
    disabled = set(session.config.disabled_checks)
    if not session.has_tests:
        disabled.update(_TEST_CHECKS)
    disabled -= _UNDISABLEABLE
    return [check(session) for check in CHECKS if check.__name__ not in disabled]


def has_verifiable_activity(session: Session) -> bool:
    """Whether an automatic Stop verdict has meaningful work to report.

    Turn-scoped, else a read-only turn re-reports long-committed work as its own. A claim
    counts: an MCP-only turn has no edits or runners, but its claim is the thing to check.
    """
    return bool(
        session.turn_edits or _runner_events(session.turn_events) or _claimed_families(session)
    )


# --- the facade -------------------------------------------------------------
#
# Names other modules and the tests reach for through `checks.`. Listed explicitly rather
# than star-imported so this file states the package's whole surface in one place.

from .cmdread import (  # noqa: E402
    _MAX_CMD_LEN as _MAX_CMD_LEN,
    _MAX_UNWRAP_DEPTH as _MAX_UNWRAP_DEPTH,
    _SHELL_TOOLS as _SHELL_TOOLS,
    _covers as _covers,
    _exec_argv as _exec_argv,
    _is_discovery as _is_discovery,
    _is_runner as _is_runner,
    _normalize_segment as _normalize_segment,
    _runner_segment as _runner_segment,
    _runner_span as _runner_span,
    _runner_family as _runner_family,
    _selection as _selection,
    _selects_whole_suite as _selects_whole_suite,
    _unwrap as _unwrap,
    written_paths as written_paths,
)
from .common import (  # noqa: E402
    _is_in_repo as _is_in_repo,
    _is_prose_path as _is_prose_path,
    _is_source_path as _is_source_path,
    _is_test_path as _is_test_path,
    _r as _r,
    _scope as _scope,
    _short as _short,
)
from .freshness import _clock_horizon as _clock_horizon  # noqa: E402
from .integrity import _is_protected as _is_protected  # noqa: E402
from .outcome import (  # noqa: E402
    _captured_output as _captured_output,
    _exec_run_for as _exec_run_for,
    _last_green_run_ts as _last_green_run_ts,
    _outcome as _outcome,
    _ran_less_than_claimed as _ran_less_than_claimed,
    _runner_events as _runner_events,
    _status_is_masked as _status_is_masked,
    _unresolved_reds as _unresolved_reds,
    unmask as unmask,
)
from .prose import _claimed_families as _claimed_families  # noqa: E402

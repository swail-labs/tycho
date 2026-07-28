"""Read a test runner's own verdict out of its terminal output — the fallback for when the
exit code isn't available. Failure is checked before success because a red summary names
both ("1 failed, 76 passed in 1.07s").

This fallback carries more weight than it looks. Agents pipe: `dotnet test 2>&1 | tail -30`,
`gradle test 2>&1 | tail -60`, `dart test 2>&1 | tail -100`. A pipe discards the runner's exit
status, so the summary line is the only evidence left, and across the dogfood sessions that
pipe was the most common reason an honest turn came back INDETERMINATE. It was never evasion:
the output is long and `tail` is the obvious thing to type.

Every pattern here is pinned to output captured from a real run (`tests/fixtures/runner_output.py`)
rather than to a manual's example, because the two disagree. `dart test` ends `+2: All tests
passed!` and never counts failures; `bun test` splits `2 pass` and `0 fail` across lines; deno
wraps its verdict in ANSI colour even through a pipe; cargo says `test result: ok.` where the
word "passed" appears only in a clause that also says "0 failed". Patterns written from
documentation match none of those.

`.pytest_cache/v/cache/lastfailed` looks like a better source and is not: pytest only
*removes* an entry when that test runs and passes, so deselected/renamed/deleted tests
persist forever. Measured here, it named a test that no longer exists and survived a fully
green 316-test run untouched. Trusting it would report FAILED on a green repo.
"""

from __future__ import annotations

import re

# Colour survives a pipe — deno emits `\x1b[32mok\x1b[0m | 2 passed | 0 failed` — so every
# pattern below would otherwise have to spell the escapes out. Strip once instead.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# `[1-9]` throughout, never `\d`: a count of zero is a runner reporting success.
_FAILURE = re.compile(
    r"""
      \b[1-9]\d*\ (?:failed|failures?|errors?|failing)\b   # pytest, deno, vitest, minitest, mocha
    | \b[1-9]\d*\ tests?\ (?:failed|error)                 # ctest, junit-platform-console
    | ^\s*[1-9]\d*\ fail\b                                 # bun
    | ^\#\ fail\ [1-9]                                     # node --test
    | (?:Failures|Errors):\s*[1-9]                         # maven surefire
    | ^Failed!                                             # dotnet / vstest
    | \btest\ result:\ FAILED\b                            # cargo
    | ^(?:FAIL\b|---\ FAIL:|FAILED\b)                      # go, deno's headline
    | ^BUILD\ (?:FAILED|FAILURE)\b                         # gradle, maven
    | ^(?:FAILURES!|ERRORS!)                               # phpunit
    | ^Failing\ tests:                                     # dart, which prints no red count
    | \bwith\ [1-9]\d*\ failures?\b                        # XCTest
    | =+\ ERRORS\ =+                                       # pytest's error block
    """,
    re.MULTILINE | re.VERBOSE,
)

# Deliberately narrower than the failure set. A missed pass costs an INDETERMINATE; a wrongly
# recognized pass costs the one thing this codebase promises never to do. So each alternative
# carries its runner's own framing as well as a count, and a stray "5 passed" in prose — or in
# an agent's summary of what it believes happened — matches nothing here.
_SUCCESS = re.compile(
    r"""
      \b\d+\ passed\b.*?\bin\ \d+(?:\.\d+)?s\b             # pytest
    | \bok\ \|\ \d+\ passed\ \|\ 0\ failed\b               # deno
    | \btest\ result:\ ok\.                                # cargo
    | ^ok\s+\S+\s+[\d.]+s                                  # go: `ok \t slug \t 0.151s`
    | ^Passed!                                             # dotnet / vstest
    | ^BUILD\ (?:SUCCESSFUL|SUCCESS)\b                     # gradle, maven
    | \bAll\ tests\ passed!                                # dart
    | ^Ran\ \d+\ tests?\ across\ \d+\ files?\.             # bun
    | \bTests?:?\s+\d+\ passed\b                           # jest, vitest
    | \b\d+\ passing\b                                     # mocha
    | \b\d+\ runs,\ \d+\ assertions,\ 0\ failures,\ 0\ errors  # minitest
    | \b\d+\ examples?,\ 0\ failures\b                     # rspec
    | ^OK\ \(\d+\ tests?                                   # phpunit
    | ^Ran\ \d+\ tests?\ in\ [\d.]+s\s*\nOK\b              # python -m unittest: `Ran`, then `OK`

    | \b100%\ tests\ passed\b                              # ctest
    | \bExecuted\ \d+\ tests?,\ with\ 0\ failures\b        # XCTest
    | \b\d+\ tests\ successful\b                           # junit-platform-console
    | ^\#\ fail\ 0\b                                       # node --test
    """,
    re.MULTILINE | re.VERBOSE,
)


def outcome(text: str) -> bool | None:
    """True = runner reported failures, False = success, None = no recognized summary.

    None is the common case: no summary means no verdict, never an assumed pass.
    """
    if not text:
        return None
    text = _ANSI.sub("", text)
    if _FAILURE.search(text):
        return True
    if _SUCCESS.search(text):
        return False
    return None

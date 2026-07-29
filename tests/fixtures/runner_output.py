"""Real runner output, captured from dogfood sessions — the evidence `runlog` is written to.

Each entry is the tail of what a runner actually printed in a headless Sonnet session against
a throwaway project, copied verbatim (ANSI escapes included where the runner emitted them
through a pipe). `PASSING` ran green, `FAILING` ran red.

Verbatim matters: a pattern written from a manual's example is a claim about a runner nobody
checked. `dart test` never prints a failure count on a green run, `bun test` splits its counts
across lines, `cargo` says "ok." where "passed" only appears beside "0 failed", and deno
colours its verdict even when the output is piped. Each of those broke a plausible pattern.

Adding a runner: paste what it printed, don't paraphrase — the whitespace and the ANSI are
part of what `runlog` has to survive.
"""

PASSING = {
    "pytest": "=========== 1380 passed in 111.28s (0:01:51) ============",
    # stdlib unittest prints its verdict on its own line, after a `Ran` line that is identical
    # on a red run — neither line alone is a verdict, which is why the pattern spans both.
    "unittest": "..\n" + "-" * 70 + "\nRan 2 tests in 0.000s\n\nOK",
    "go": "ok  \tslug\t0.151s",
    "cargo": (
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.00s"
    ),
    # An ignored test still reports `ok` — the silenced test is `skip_mock_injection`'s
    # problem, not this module's. Reading it as a failure here would cry wolf on every
    # legitimately-ignored test in the repo.
    "cargo_with_ignored": (
        "test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; "
        "finished in 0.00s"
    ),
    "dotnet": (
        "A total of 1 test files matched the specified pattern.\n"
        "Passed!  - Failed:     0, Passed:     2, Skipped:     0, Total:     2, "
        "Duration: 3 ms - Slug.Tests.dll (net10.0)"
    ),
    "dart": (
        "00:00 +0: loading test/slug_test.dart\n"
        "00:00 +1: test/slug_test.dart: truncate\n"
        "00:00 +2: All tests passed!"
    ),
    "dart_with_skip": (
        "00:00 +1: test/slug_test.dart: accents\n"
        "  Skip: accent folding out of scope this sprint\n"
        "00:00 +1 ~1: All tests passed!"
    ),
    "deno": (
        "\x1b[0m\x1b[38;5;245mrunning 2 tests from ./slug_test.ts\x1b[0m\n"
        "basic ... \x1b[0m\x1b[32mok\x1b[0m \x1b[0m\x1b[38;5;245m(292µs)\x1b[0m\n"
        "\x1b[0m\x1b[32mok\x1b[0m | 2 passed | 0 failed \x1b[0m\x1b[38;5;245m(1ms)\x1b[0m"
    ),
    "bun": "  2 pass\n  0 fail\n  2 expect() calls\nRan 2 tests across 1 file. [64.00ms]",
    "gradle": "> Task :test\nBUILD SUCCESSFUL in 3s\n4 actionable tasks: 4 executed",
    "minitest": (
        "Finished in 0.000856s, 3504.6729 runs/s, 3504.6729 assertions/s.\n"
        "3 runs, 3 assertions, 0 failures, 0 errors, 0 skips"
    ),
    "minitest_with_skip": (
        "2 runs, 1 assertions, 0 failures, 0 errors, 1 skips\n"
        "You have skipped tests. Run with --verbose for details."
    ),
}

FAILING = {
    "pytest": "=========== 1 failed, 1379 passed in 109.44s (0:01:49) ============",
    "unittest": (
        "FAIL: test_accents (test_slug.TestSlug.test_accents)\n"
        + "-" * 70 + "\nRan 2 tests in 0.001s\n\nFAILED (failures=1)"
    ),
    "go": "    slug_test.go:13: got \"café-bar\"\nFAIL\nFAIL\tslug\t0.147s\nFAIL",
    "cargo": (
        "failures:\n    tests::accents\n"
        "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.00s\n"
        "error: test failed, to rerun pass `--lib`"
    ),
    "dotnet": (
        "  Failed SluggerTests.Accents [3 ms]\n"
        "Failed!  - Failed:     1, Passed:     1, Skipped:     0, Total:     2, "
        "Duration: 4 ms - Slug.Tests.dll (net10.0)"
    ),
    "dart": (
        "Failing tests:\n  test/slug_test.dart: accents\n"
        "Consider enabling the flag chain-stack-traces to receive more detailed exceptions."
    ),
    "deno": (
        "\x1b[0m\x1b[1m\x1b[37m\x1b[41m FAILURES \x1b[0m\n"
        "accents \x1b[0m\x1b[38;5;245m=> ./slug_test.ts:8:6\x1b[0m\n"
        "\x1b[0m\x1b[31mFAILED\x1b[0m | 1 passed | 1 failed \x1b[0m\x1b[38;5;245m(13ms)\x1b[0m\n"
        "\x1b[0m\x1b[1m\x1b[31merror\x1b[0m: Test failed"
    ),
    "gradle": (
        "> Run with --scan to get full insights.\n"
        "> Get more help at https://help.gradle.org.\n"
        "BUILD FAILED in 21s\n1 actionable task: 1 executed"
    ),
    "minitest": (
        "SlugTest#test_accents [test/slug_test.rb:10]:\n"
        "Expected: \"cafe-bar\"\n  Actual: \"café-bar\"\n"
        "2 runs, 2 assertions, 1 failures, 0 errors, 0 skips"
    ),
}

# What must NOT read as a verdict: an agent's own prose, and a runner that never got to run.
# The second is the reason `outcome` returns None rather than guessing — a toolchain that
# cannot build has no verdict to report, and calling that a failure blames the wrong thing.
NOT_A_VERDICT = {
    "agent_prose": "I ran the suite and all 5 passed, so the change is good.",
    "toolchain_broken": (
        "12 | import Foundation\n"
        "   |        `- error: failed to build module 'Foundation' for importation"
    ),
    "msbuild_no_project": (
        "MSBUILD : error MSB1003: Specify a project or solution file. "
        "The current working directory does not contain a project or solution file."
    ),
    # A hand-rolled `make test` recipe ending in `echo PASS`. There is no runner summary here
    # at all — the word is the project's own, and matching it would hand any Makefile a green.
    "handrolled_make": (
        "cc -o /tmp/test_slug slug.c tests/test_slug.c && /tmp/test_slug && echo PASS\nPASS"
    ),
    "empty": "",
}

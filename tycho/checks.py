"""The deterministic checks. Each is a pure function of the Session snapshot returning one
CheckResult. A check that doesn't apply returns UNSUPPORTED with a reason — never a false FAIL.
`run_checks` drives the registry and honours `config.disabled_checks`.
"""

from __future__ import annotations

import re
import shlex
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from . import runlog
from .astdiff import assertion_delta, skip_or_mock_added
from .model import CheckResult, CheckStatus, Session

_TEST_RUNNERS = (
    "python -m pytest",
    "python3 -m pytest",
    "python -m unittest",
    "poetry run pytest",
    "uv run pytest",
    "pytest",
    "go test",
    "npm test",
    "npm run test",
    "cargo test",
    "make test",
    "make check",
    "jest",
    "vitest",
    "mocha",
    "npx jest",
    "npx vitest",
    "npx mocha",
    "yarn test",
    "pnpm test",
    "mvn test",
    "mvnw test",
    "gradle test",
    "gradlew test",
    "dotnet test",
    "ctest",
    "meson test",
    "bazel test",
    "phpunit",
    "pest",
    "codecept",
    "codeception",
    "composer test",
    "rspec",
    "bundle exec rspec",
    "rake test",
    "swift test",
    "xcodebuild test",
    "dart test",
    "flutter test",
    "tox",
    "deno test",
    "bun test",
    "hatch run test",
)

# Flags that make a runner exit 0 without running the suite — read as a pass they fabricate a
# green. Whole tokens only: `-n` is xdist parallelism and `-v` is verbose, neither belongs here.
_DISCOVERY_FLAGS = frozenset({
    "--collect-only", "--co", "--no-run", "--listtests", "--list-tests", "--list",
    "--dry-run", "--version", "-V", "--help", "-h", "--fixtures", "--markers",
})
# `tox -e lint` is a linter. Only an env naming itself a test env counts.
_TOX_TEST_ENV = re.compile(r"py[\d.]*$|test|unit|integration", re.IGNORECASE)

# Not Bash-only — a PowerShell/Shell tool runs `pytest` just the same.
_SHELL_TOOLS = frozenset({"Bash", "Shell", "sh", "PowerShell", "powershell", "pwsh"})

# Ephemeral-env wrappers put flags between themselves and the runner, defeating a prefix match.
# Only *multi-word* phrases count later in the segment — a phrase is never a `--with` value.
_RUN_WRAPPERS = ("uv run", "uvx", "poetry run", "pdm run", "hatch run", "rye run", "npx", "pnpm dlx", "bunx")
_PHRASE_RUNNERS = tuple(r for r in _TEST_RUNNERS if " " in r)
_BARE_RUNNERS = frozenset(r for r in _TEST_RUNNERS if " " not in r)
# Wrapper flags whose next token installs rather than runs: `uv run --with pytest ruff check`
# is not a test run. Everything else is scanned past — an allowlist of every flag uv might
# grow is one we'd always be behind, and being behind it hid `uv run --group test pytest`.
_INSTALL_VALUE_FLAGS = frozenset({
    "--with", "--from", "--with-editable", "--with-requirements", "-p", "--package",
})

# `<python> -m <module>` — the module IS the runner, so `"$PY" -m pytest` stays visible.
_MODULE_RUNNERS = ("pytest", "unittest", "nose2")

# Wrappers carrying the real command inside an argument; see `_unwrap`.
_DASHDASH_WRAPPERS = ("wsl", "env")
_C_SHELLS = ("bash", "sh", "zsh", "dash", "ash")
_EXE_SUFFIX = re.compile(r"\.(?:exe|bat|cmd|ps1)$", re.IGNORECASE)


def _looks_like_interpreter(tok: str) -> bool:
    """A real name (`python3.12`) or an unresolved variable (`$PY`). Keeps the `-m pytest`
    rule off `echo -m pytest`."""
    if tok[:1] in "$%{":
        return True
    name = tok.lower()
    return name == "py" or bool(re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", name))

# Segment a shell command, so a runner name inside a quoted echo/grep argument doesn't count.
_SEGMENT_SEP = re.compile(r"&&|\|\||[;|\n()]")
# Same separators, captured: `_status_is_masked` needs to know *which* one follows.
_SEGMENT_TOKENS = re.compile(r"(&&|\|\||[;|\n()])")
_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S+\s+)+")

# pytest's verdict is the last line; the slack absorbs the trailing warnings block. Small on
# purpose — further up we'd be reading the run, not its conclusion.
_SUMMARY_TAIL_LINES = 12


def _normalize_segment(segment: str) -> str:
    """Strip env prefixes and the leading path, so `.venv/bin/python -m pytest` reads as
    `python -m pytest`."""
    segment = _ENV_PREFIX.sub("", segment.strip())
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = []
    if parts:
        exe = parts[0].rsplit("/", 1)[-1]
        exe = re.sub(r"\.(?:exe|bat|cmd|ps1)$", "", exe, flags=re.IGNORECASE)
        segment = " ".join([exe, *parts[1:]])
    return segment


def _is_discovery(segment: str) -> bool:
    """True when the segment lists, compiles or prints instead of running the suite —
    `pytest --collect-only`, `cargo test --no-run`, `tox -e lint` all exit 0 proving nothing."""
    tokens = segment.split()
    if any(t.split("=", 1)[0].lower() in _DISCOVERY_FLAGS for t in tokens):
        return True
    if tokens and tokens[0] == "tox":
        envs = next(
            (t.split("=", 1)[1] if "=" in t else nxt
             for t, nxt in zip(tokens, tokens[1:] + [""])
             if t in ("-e", "--env") or t.startswith(("-e=", "--env="))),
            None,
        )
        if envs is not None:
            return not any(_TOX_TEST_ENV.search(e) for e in envs.split(","))
    return False


def _is_runner(segment: str) -> bool:
    """True if this already-normalized segment invokes a test/build runner."""
    if _is_discovery(segment):
        return False
    if segment.startswith("java ") and ("junit" in segment.lower() or "testng" in segment.lower()):
        return True
    return _runner_span(segment) is not None


def _runner_span(segment: str) -> tuple[int, int] | None:
    """Token span of the runner phrase within a normalized segment, or None.

    One locator answers both "is this a runner" and "where do its arguments start" — a second
    matcher that only knew the `_TEST_RUNNERS` spellings read `uv run --with pytest pytest -q`
    as having no runner, silently making every scope question about wrappers unanswerable.
    """
    tokens = segment.split()
    if not tokens:
        return None
    # Longest direct match wins, so `python -m pytest` isn't read as the shorter `pytest`.
    longest = 0
    for runner in _TEST_RUNNERS:
        phrase = runner.split()
        if tokens[:len(phrase)] == phrase:
            longest = max(longest, len(phrase))
    if longest:
        return 0, longest
    # `<interpreter> -m pytest`, guarded so `echo -m pytest` doesn't count.
    if (
        len(tokens) >= 3
        and tokens[1] == "-m"
        and tokens[2] in _MODULE_RUNNERS
        and _looks_like_interpreter(tokens[0])
    ):
        return 0, 3
    wrapper = next((w for w in _RUN_WRAPPERS if segment == w or segment.startswith(f"{w} ")), None)
    if wrapper is None:
        return None
    start = len(wrapper.split())
    # A multi-word runner phrase anywhere after the wrapper (`uv run … python -m pytest`).
    for i in range(start, len(tokens)):
        for runner in _PHRASE_RUNNERS:
            phrase = runner.split()
            if tokens[i:i + len(phrase)] == phrase:
                return i, i + len(phrase)
    # The wrapper's own command. In `uv run --with pytest pytest -q` the first "pytest" is an
    # install argument and the second is the command, so install-flag values are skipped and a
    # token after an unknown flag stays ambiguous until the next one resolves it.
    prev_was_flag = False
    skip_next = False
    for i in range(start, len(tokens)):
        token = tokens[i]
        if skip_next:
            skip_next = prev_was_flag = False
            continue
        if token.startswith("-"):
            skip_next = token in _INSTALL_VALUE_FLAGS and "=" not in token
            prev_was_flag = not skip_next
            continue
        if token in _BARE_RUNNERS:
            return i, i + 1
        if not prev_was_flag:
            return None  # this is the wrapper's command, and it isn't a runner
        prev_was_flag = False  # an unknown flag's value, or the command — the next one decides
    return None


# These strings are agent-controlled and unvalidated upstream. Measured: a 1M-char token costs
# ~14s in shlex, and 500 levels of `env -- env -- ...` ~1s. Both bounds sit far above any real
# invocation (a few hundred chars, one or two wrappers).
_MAX_CMD_LEN = 4096
_MAX_UNWRAP_DEPTH = 10


_TIMEOUT_DURATION = re.compile(r"[\d.]+[smhd]?")
# `timeout`'s own options that consume the next argument when not written as `--flag=value`.
_TIMEOUT_VALUE_FLAGS = ("-k", "--kill-after", "-s", "--signal")


def _after_timeout_duration(args: list[str]) -> list[str]:
    """The wrapped command in `timeout [OPTION...] DURATION COMMAND [ARG...]`, else [].

    Only `timeout`'s own leading options are skipped and the scan stops at the duration.
    Filtering every `-flag` out of the whole list — what this replaced — deleted the wrapped
    command's flags too: `timeout 60 pytest --collect-only` unwrapped to a bare `pytest`, and
    Tycho read a collect-only as a passing suite.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in _TIMEOUT_VALUE_FLAGS:
            i += 1  # its value is the next token, not the duration
        i += 1
    if i + 1 >= len(args) or not _TIMEOUT_DURATION.fullmatch(args[i]):
        return []
    return args[i + 1:]


def _unwrap(segment: str) -> str | None:
    """The inner command a wrapper carries in an argument, else None. Peels one layer; callers
    recurse. Takes the RAW segment so a quoted `-c '<cmd>'` stays a single token."""
    try:
        parts = shlex.split(segment)
    except ValueError:
        return None
    if not parts:
        return None
    head = _EXE_SUFFIX.sub("", parts[0].rsplit("/", 1)[-1]).lower()

    # `tycho run|exec [--] <cmd>` — both forward <cmd>'s real exit code; `exec` also logs it.
    if head == "tycho" and len(parts) >= 2 and parts[1] in ("run", "exec"):
        rest = parts[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return shlex.join(rest) if rest else None

    # `<wrapper> ... -- <cmd...>` (`wsl.exe -d Ubuntu -- <cmd>`, `env FOO=bar -- <cmd>`).
    if head in _DASHDASH_WRAPPERS and "--" in parts:
        rest = parts[parts.index("--") + 1 :]
        return shlex.join(rest) if rest else None

    # `timeout [flags] <duration> <cmd...>` — agents wrap long suites in it routinely.
    if head == "timeout":
        rest = _after_timeout_duration(parts[1:])
        if rest:
            return shlex.join(rest)

    # `<shell> ... -c '<cmd>'`
    if head in _C_SHELLS:
        for i in range(1, len(parts) - 1):
            flag = parts[i]
            if flag.startswith("-") and "c" in flag.lstrip("-").lower():
                return parts[i + 1]

    # `ssh <host> <cmd...>` — no-option form only, so an option value can't be read as the host.
    if head == "ssh" and len(parts) >= 3 and not parts[1].startswith("-"):
        return shlex.join(parts[2:])

    # `docker run` isn't unwrapped: the image and its flags need skipping first. Add when a
    # docker test workflow needs it.
    return None


def _runner_segment(cmd: str, _depth: int = 0) -> str | None:
    """The command segment that is a test/build runner, or None.

    Past the size/depth bounds, None is the safe direction: it reads as "no evidence of a test
    run", which is what an unparseable command should mean.
    """
    if len(cmd) > _MAX_CMD_LEN or _depth > _MAX_UNWRAP_DEPTH:
        return None
    for segment in _SEGMENT_SEP.split(cmd):
        norm = _normalize_segment(segment)
        if _is_runner(norm):
            return norm
        inner = _unwrap(segment)
        if inner is not None:
            found = _runner_segment(inner, _depth + 1)
            if found is not None:
                return found
    return None


def _exec_argv(cmd: str, _depth: int = 0) -> list[str] | None:
    """The argv `tycho exec` was given inside `cmd`, or None — the join between the two
    evidence streams, the only thing the transcript and the exec log both know."""
    if len(cmd) > _MAX_CMD_LEN or _depth > _MAX_UNWRAP_DEPTH:
        return None
    for segment in _SEGMENT_SEP.split(cmd):
        try:
            parts = shlex.split(segment)
        except ValueError:
            continue
        if not parts:
            continue
        head = _EXE_SUFFIX.sub("", parts[0].rsplit("/", 1)[-1]).lower()
        if head == "tycho" and len(parts) >= 2 and parts[1] == "exec":
            rest = parts[2:]
            if rest and rest[0] == "--":
                rest = rest[1:]
            if rest:
                return rest
        inner = _unwrap(segment)
        if inner is not None:
            found = _exec_argv(inner, _depth + 1)
            if found is not None:
                return found
    return None


# Slack between the harness's event clock and ours. Generous on purpose — the ambiguity rule
# below is what protects correctness, not this number.
_EXEC_CLOCK_SLACK = 5.0


def _exec_run_for(event, commands) -> "CommandRun | None":  # noqa: F821 — model.CommandRun
    """The `tycho exec` evidence for this transcript event, or None. Matched on the inner argv,
    and **an ambiguous match is no match**.

    `commands.jsonl` is shared by every process in the repo, so `tycho exec -- pytest -q`
    appears twice when two agents work one tree. Picking the newest let agent B's pass answer
    for agent A's failure — VERIFIED on a red suite, citing the strongest evidence there is.

    ponytail: argv + a time window, no process identity. `tycho exec` could stamp its pid if
    the window proves too coarse.
    """
    if not commands:
        return None
    argv = _exec_argv(event.input.get("command") or "")
    if not argv:
        return None
    # The event is stamped when the tool *finished*; a run that began after that is a
    # different run.
    latest = (event.ts or 0.0) + _EXEC_CLOCK_SLACK
    matches = []
    for run in commands:
        try:
            if shlex.split(run.cmd) != argv:
                continue
        except ValueError:
            continue
        if event.ts and run.started_at > latest:
            continue  # a later run, by us or by another agent in this repo
        matches.append(run)
    return matches[-1] if len(matches) == 1 else None


def _status_is_masked(cmd: str, _depth: int = 0) -> bool:
    """True when the exit status the harness recorded is *not* the runner's own.

        pytest | tail -1      the pipeline's status is tail's (no pipefail here)
        pytest; echo done     `;` discards what came before
        pytest || true        the failure is swallowed by construction

    `&&` is safe and must NOT be flagged. A wrapper is masked when its inner command is.

    When in doubt say masked and let `_outcome` fall back to the runner's own output — that
    includes giving up past the bounds, where a masking operator could hide beyond where we
    stopped looking. The opposite give-up direction from `_runner_segment`.
    """
    if len(cmd) > _MAX_CMD_LEN or _depth > _MAX_UNWRAP_DEPTH:
        return True
    parts = _SEGMENT_TOKENS.split(cmd)  # [segment, sep, segment, sep, ..., segment]
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not _is_runner(_normalize_segment(seg)):
            inner = _unwrap(seg)
            if inner is None or _runner_segment(inner) is None:
                continue  # not the runner segment, wrapped or otherwise
            if _status_is_masked(inner, _depth + 1):
                return True  # a masking operator inside the wrapper hides the status
        # The first separator that redirects, swallows, or supersedes the status masks it.
        for j in range(i + 1, len(parts), 2):
            sep = parts[j]
            rest = parts[j + 1] if j + 1 < len(parts) else ""
            if sep in ("|", "||"):
                return True  # status replaced downstream, or the failure swallowed
            if sep in (";", "\n") and rest.strip():
                return True  # something ran after; its status is what got recorded
        return False  # nothing after it can overwrite the status — trust the exit code
    return False  # no runner in this command at all


def _captured_output(event) -> str:
    """The runner's own words, tail-first — or "" when the harness kept none.

    Tail only: Claude Code caps `toolUseResult.stdout` at 30k and keeps the *head* (checked
    against 2356 real payloads) while pytest prints its summary last, so a truncated capture
    reports nothing rather than matching a stray "5 passed" from a red run.
    """
    result = event.result or {}
    text = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr")).strip()
    return "\n".join(text.splitlines()[-_SUMMARY_TAIL_LINES:]) if text else ""


def _outcome(event, commands=()) -> bool | None:
    """Did this runner invocation fail? True = failed, False = passed, None = can't tell.

    One predicate for every caller, so no two checks disagree about what "green" means.
    Evidence ladder, strongest first: a status Tycho captured itself (`tycho exec` read
    `wait()`), the transcript's exit code when nothing masked it, then the runner's own summary
    line. When the first two disagree, failure wins — `tycho exec -- pytest && ./deploy.sh` can
    fail for a reason the capture can't see.
    """
    run = _exec_run_for(event, commands)
    if run is not None:
        masked = _status_is_masked(event.input.get("command") or "")
        return run.failed or bool(event.is_error and not masked)
    if not _status_is_masked(event.input.get("command") or "") and event.is_error is not None:
        return event.is_error
    return runlog.outcome(_captured_output(event))


def command_execution(session: Session) -> CheckResult:
    runners = _runner_events(session.turn_events)
    if not runners:
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"no test/build command ran this {_scope(session)}",
        )
    last = max(runners, key=lambda e: e.ts)
    raw = last.input.get("command", "")
    cmd = _short(_runner_segment(raw) or raw)
    masked = _status_is_masked(raw)
    run = _exec_run_for(last, session.commands)
    outcome = _outcome(last, session.commands)
    if outcome is None:
        why = (
            "its exit status was masked by the shell" if masked
            else "no exit status was recorded"
        )
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"`{cmd}` ran but {why}, and its output carries no summary — Tycho can't confirm it passed"
            " (prefix it with `tycho exec --` to put its real status on the record)",
        )
    # Name the source: output-recovered evidence is weaker than an exit code.
    if run is not None:
        via = f" (Tycho ran it — exit {run.exit_code})"
    else:
        via = " (read from its output — exit status masked by the shell)" if masked else ""
    if outcome:
        return _r("command_execution", CheckStatus.FAIL, f"`{cmd}` ran but reported an error{via}")
    unresolved = [e for e in _unresolved_reds(session.turn_events, session.commands) if e.ts < last.ts]
    if unresolved:
        red = _short(_runner_segment(unresolved[0].input.get("command", "")) or "")
        return _r(
            "command_execution",
            CheckStatus.UNSUPPORTED,
            f"`{red}` reported an error earlier this {_scope(session)} and was never re-run —"
            f" `{cmd}` passed but is a different command, so it can't stand in for it",
        )
    return _r("command_execution", CheckStatus.PASS, f"`{cmd}` ran without error{via}")


def _unresolved_reds(events, commands) -> list:
    """Failed runner invocations that no later green run covers.

    Run the suite, see red, narrow to the failing file, go green, stop — the standard agent
    loop, and taking that last success at face value reported VERIFIED. So a green supersedes a
    red only when it ran *at least as much*: the same command, or the whole suite.

    Identity alone was its own bug — a re-run with anything but byte-identical argv left the
    red unresolved, pinning the `test_*` checks adverse with no way to discharge them.
    """
    runs = sorted(_runner_events(events), key=lambda e: e.ts)
    reds = []
    for e in runs:
        if _outcome(e, commands) is not True:
            continue
        red_cmd = _runner_segment(e.input.get("command") or "")
        if any(
            later.ts > e.ts
            and _outcome(later, commands) is False
            and _covers(_runner_segment(later.input.get("command") or ""), red_cmd)
            for later in runs
        ):
            continue
        reds.append(e)
    return reds


def _covers(green: str | None, red: str | None) -> bool:
    """Did `green` run at least everything `red` did? Both are normalized runner segments."""
    if green is None or red is None:
        return False
    if green == red:
        return True
    # Different runners are different suites: a green `pytest` says nothing about a red `npm test`.
    return (
        _runner_family(green) is not None
        and _runner_family(green) == _runner_family(red)
        and _selects_whole_suite(green) is True
    )


# Runner families sharing an argument grammar. Anything unlisted answers "can't tell".
_FAMILIES = (
    ("pytest", ("pytest",)),
    ("unittest", ("unittest",)),
    ("go", ("go test",)),
    ("cargo", ("cargo test",)),
    ("tox", ("tox",)),
    ("js", ("jest", "vitest", "mocha", "npm test", "npm run test", "yarn test", "pnpm test")),
)

# Options taking a separate value — needed only to tell one from a positional test selector.
# `pytest -n 4` runs everything on 4 workers, not a test called "4".
_VALUE_OPTIONS = {
    "pytest": frozenset({"-k", "-m", "-n", "-p", "-c", "-o", "-W", "--tb", "--maxfail",
                         "--cov", "--rootdir", "--deselect", "--ignore", "--junitxml",
                         "--log-level", "--durations", "--dist", "--numprocesses"}),
    "unittest": frozenset({"-k"}),
    "go": frozenset({"-run", "-timeout", "-count", "-parallel", "-tags", "-coverprofile"}),
    "cargo": frozenset({"--test", "--bin", "--package", "-p", "--features", "--target",
                        "--manifest-path", "--jobs", "-j"}),
    "tox": frozenset({"-e", "-c", "--workdir"}),
    "js": frozenset({"-t", "--testNamePattern", "--testPathPattern", "--maxWorkers",
                     "--reporters", "--config", "-c"}),
}

# Options that narrow the run to part of the suite. A run carrying one is never whole-suite.
_NARROWING_OPTIONS = {
    "pytest": frozenset({"-k", "-m", "--deselect", "--ignore", "--last-failed", "--lf",
                         "--failed-first", "--ff", "--stepwise", "--sw"}),
    "unittest": frozenset({"-k"}),
    "go": frozenset({"-run"}),
    "cargo": frozenset({"--test", "--bin", "--lib", "--bins", "--doc", "--example"}),
    "tox": frozenset({"-e"}),
    "js": frozenset({"-t", "--testNamePattern", "--testPathPattern", "--onlyFailures",
                     "--onlyChanged", "--changedSince", "--findRelatedTests"}),
}

# Positionals that still mean "everything" — `go test ./...` is the whole module.
_WHOLE_SUITE_POSITIONALS = {
    "go": frozenset({"./...", "all"}),
    "cargo": frozenset(),
    "pytest": frozenset(),
    "unittest": frozenset({"discover"}),
    "tox": frozenset(),
    "js": frozenset(),
}


def _runner_family(segment: str) -> str | None:
    """Which argument grammar this normalized runner segment speaks, or None if unmodelled."""
    span = _runner_span(segment)
    if span is None:
        return None
    phrase = segment.split()[span[0]:span[1]]
    for family, markers in _FAMILIES:
        # Token sublists, never substrings: `"go test" in "cargo test"` is true.
        for marker in markers:
            mt = marker.split()
            if any(phrase[i:i + len(mt)] == mt for i in range(len(phrase) - len(mt) + 1)):
                return family
    return None


def _selects_whole_suite(segment: str) -> bool | None:
    """True = the whole suite, False = narrowed, None = the arguments can't be read.

    None is an answer, not a failure mode: an unrecognized option followed by a bare word could
    be that option's value or a test selector. Callers treat None as "does not supersede".
    """
    family = _runner_family(segment)
    span = _runner_span(segment)
    if family is None or span is None:
        return None
    value_opts = _VALUE_OPTIONS[family]
    narrowing = _NARROWING_OPTIONS[family]
    whole = _WHOLE_SUITE_POSITIONALS[family]

    tokens = segment.split()[span[1]:]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":  # `npm test -- <runner args>`
            i += 1
            continue
        if not token.startswith("-"):
            if token in whole:
                i += 1
                continue
            return False  # a test path, node id or pattern
        name = token.split("=", 1)[0]
        if name in narrowing:
            return False
        if "=" in token:  # self-contained, never consumes the next token
            i += 1
            continue
        if name in value_opts:
            i += 2
            continue
        # An unmodelled option: a bare word after it could be its value or a test selector,
        # and guessing either way invents evidence.
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            return None
        i += 1
    return True


def test_freshness(session: Session) -> CheckResult:
    green_ts = _last_green_run_ts(session)
    if green_ts is None:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no passing test run to check against")
    source_edits = [fe for fe in session.edits if _is_source_path(fe.path)]
    if not source_edits:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no source edits to check against the run")
    horizon = _clock_horizon(session)
    stale, skewed = [], []
    for fe in source_edits:
        fs = session.files.get(fe.path)
        if not (fs and fs.mtime is not None):
            continue
        if fs.mtime > horizon:
            skewed.append(fe.path)
        elif fs.mtime > green_ts:
            stale.append((fe.path, fs.mtime))
    if stale:
        path, mt = max(stale, key=lambda x: x[1])
        # Session-scoped: an uncovered source from an earlier turn still counts, but the
        # wording must not imply a this-turn edit.
        if path in {fe.path for fe in session.turn_edits}:
            evidence = f"{path} edited {int(mt - green_ts)}s after the last passing test run"
        else:
            evidence = f"{path} still uncovered since the last passing run (last edited in an earlier turn)"
        return _r("test_freshness", CheckStatus.STALE, evidence)
    if skewed:
        return _r(
            "test_freshness",
            CheckStatus.UNSUPPORTED,
            f"{sorted(skewed)[0]} has an mtime in the future (clock skew, or a copied/extracted"
            " file) — Tycho can't tell whether it postdates the last passing run",
        )
    return _r("test_freshness", CheckStatus.PASS, "sources unchanged since the last passing run")


# An mtime this far past everything the session recorded came from a tarball or a skewed
# clock, not an edit — read as one it pins the repo at STALE forever. Measured against the
# session's own latest timestamp, never the wall clock, so checks stay pure.
_CLOCK_SLACK = 3600.0


def _clock_horizon(session: Session) -> float:
    stamps = [e.ts for e in session.events]
    stamps += [fe.ts for fe in session.edits]
    stamps += [m.ts for m in session.messages]
    stamps += [c.ended_at for c in session.commands]
    return max(stamps, default=0.0) + _CLOCK_SLACK


def test_provenance(session: Session) -> CheckResult:
    test_edits = [fe for fe in session.edits if _is_test_path(fe.path)]
    if not test_edits:
        return _r("test_provenance", CheckStatus.UNSUPPORTED, "no test files touched this session")
    green_ts = _last_green_run_ts(session)
    if green_ts is None:
        return _r("test_provenance", CheckStatus.UNSUPPORTED, "no passing test run to check against")
    after = [(fe.path, fe.ts) for fe in test_edits if fe.ts > green_ts]
    if after:
        path, ts = max(after, key=lambda x: x[1])
        return _r(
            "test_provenance",
            CheckStatus.FAIL,
            f"{path} modified {int(ts - green_ts)}s after the last passing run — that run didn't cover it",
        )
    return _r("test_provenance", CheckStatus.PASS, "tests unchanged since the last passing run")


def assertion_weakening(session: Session) -> CheckResult:
    return _ast_check(session, "assertion_weakening", assertion_delta,
                      "no assertions removed or weakened in edited tests")


def skip_mock_injection(session: Session) -> CheckResult:
    return _ast_check(session, "skip_mock_injection", skip_or_mock_added,
                      "no skips or mocks injected into edited tests")


def file_state(session: Session) -> CheckResult:
    paths = {fe.path for fe in session.turn_edits}
    if not paths:
        return _r(
            "file_state", CheckStatus.UNSUPPORTED, f"no files created or edited this {_scope(session)}"
        )
    broken = []
    for path in sorted(paths):
        fs = session.files.get(path)
        if fs is None or not fs.exists:
            broken.append(f"{path} — missing")
        elif not (fs.current_text or "").strip():
            broken.append(f"{path} — empty")
    if broken:
        return _r("file_state", CheckStatus.FAIL, "; ".join(broken))
    return _r(
        "file_state",
        CheckStatus.PASS,
        f"all {len(paths)} file(s) edited this {_scope(session)} present on disk",
    )


def git_state(session: Session) -> CheckResult:
    if not session.git.is_repo:
        return _r("git_state", CheckStatus.UNSUPPORTED, "not a git repository")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "git_state",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to reconcile against git",
        )
    # An out-of-repo edit judged against this repo's diff reads as "reconciled" on file_state's
    # evidence (it exists on disk), not git's.
    in_repo = {p for p in edited if _is_in_repo(p)}
    outside = edited - in_repo
    if not in_repo:
        return _r(
            "git_state",
            CheckStatus.UNSUPPORTED,
            f"{len(outside)} path(s) edited outside the repo — nothing here for git to reconcile",
        )
    changed = set(session.git.changed_paths)
    # A phantom is an edit that's neither in the working diff nor on disk.
    phantom = [
        p for p in sorted(in_repo)
        if p not in changed and not (session.files.get(p) and session.files[p].exists)
    ]
    if phantom:
        return _r("git_state", CheckStatus.FAIL, f"claimed edits absent from repo: {', '.join(phantom)}")
    evidence = (
        f"{len(in_repo)} path(s) edited this {_scope(session)} reconciled with git "
        f"({len(in_repo & changed)} uncommitted)"
    )
    if outside:
        evidence += f"; {len(outside)} outside the repo — not tracked here"
    return _r("git_state", CheckStatus.PASS, evidence)


def scope_drift(session: Session) -> CheckResult:
    globs = session.config.scope_include
    excludes = session.config.scope_exclude
    if not globs:
        return _r("scope_drift", CheckStatus.UNSUPPORTED,
                  "no [scope] set — run `tycho scope add '<glob>'` to bound edits (zero-config)")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "scope_drift",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to check scope against",
        )
    def _in_scope(p: str) -> bool:
        return any(fnmatchcase(p, g) for g in globs) and not any(fnmatchcase(p, g) for g in excludes)

    outside = [p for p in sorted(edited) if not _in_scope(p)]
    if outside:
        return _r("scope_drift", CheckStatus.FAIL, f"edits outside stated scope: {', '.join(outside)}")
    within = f"all edits within scope {list(globs)}"
    return _r("scope_drift", CheckStatus.PASS, f"{within} (excluding {list(excludes)})" if excludes else within)


# tool_call_provenance families: (label, claim pattern, tool-name substrings). Broad by design —
# a claim requires *some* tool call of that family, never a content match. An unclassifiable
# claim is simply not counted.
_PROV_WEB = re.compile(
    r"\b(?:searched (?:the web|online)|web[- ]?searched|googled|"
    r"browsed to|fetched the (?:page|url|web ?site|site))\b",
    re.IGNORECASE,
)
# A ticket anchor near an action cue, in either order. Both cue kinds are past-only, so a
# future "I'll move ACME-123" can't fire: a past-tense verb, or a two-status arrow.
_ISSUE_ANCHOR = r"(?:\b[A-Z][A-Z0-9]+-\d+\b|\b[Jj]ira (?:ticket|issue|card)\b)"
_ISSUE_STATUS = r"(?:To ?Do|In Progress|In Review|Backlog|Blocked|Hold|Done)"
_ISSUE_CUE = (
    r"(?i:\b(?:created|filed|moved|transitioned|assigned|re-?opened|closed|linked|"
    rf"commented on)\b|{_ISSUE_STATUS}\s*(?:→|-+>|↔)\s*{_ISSUE_STATUS})"
)
_PROV_ISSUE = re.compile(
    rf"{_ISSUE_CUE}[^.\n]{{0,40}}?{_ISSUE_ANCHOR}|{_ISSUE_ANCHOR}[^.\n]{{0,40}}?{_ISSUE_CUE}",
)
_PROVENANCE_FAMILIES = (
    ("a web search/fetch", _PROV_WEB, ("search", "fetch", "browse", "web")),
    ("an issue-tracker action", _PROV_ISSUE, ("jira", "atlassian", "issue", "linear")),
)

# A third-party subject or pre-existing state is the agent *narrating* ("Dan closed ACME-123"),
# so those clauses are neutralized first; subject-dropped and first-person survive. Recall loss
# is accepted over a false FAIL. Case-sensitive names, so "and moved" isn't read as a subject.
_REPORTED_SUBJECT = (
    r"(?:he|she|they|"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|"                        # a name: "Dan", "Dan Mano"
    r"[Tt]he\s+(?:operator|owner|user|maintainer|reviewer|team)|"
    r"(?:was|were|been|had|has|is|are))"                      # passive / pre-existing state
)
_REPORTED_VERB = (
    r"(?i:created|filed|moved|transitioned|assigned|re-?opened|closed|linked|commented|"
    r"searched|googled|browsed|fetched)"
)
_REPORTED = re.compile(
    rf"\b{_REPORTED_SUBJECT}\s+(?:already |just |then |recently |now )?{_REPORTED_VERB}"
)

# An observed state ("the board shows In Review → Done") reports where a ticket is, not a
# transition the agent made — neutralized through its arrow. A self-claim keeps an action verb.
_STATE_VERB = r"(?i:sits?|stands?|shows?|reads?|remains?|stays?|is|are|'s|was|were)"
_REPORTED_STATE = re.compile(
    rf"(?:{_ISSUE_ANCHOR}|\bit\b|\b[Tt]he\s+(?:ticket|card|board|issue))\s+"
    rf"(?i:already |just |now |currently |still )*{_STATE_VERB}\b"
    # Bounded: unbounded, this re-scans the line from every "it is" — 192 KB cost 80s.
    rf"[^.\n]{{0,80}}?{_ISSUE_STATUS}\s*(?:→|-+>|↔)\s*{_ISSUE_STATUS}"
)


# Spans the agent is *showing*, not asserting. Dropping them removed 9 of 41 matches over this
# machine's real transcripts. Apostrophes are left alone — `39's` would swallow the sentence.
_SHOWN_SPANS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"^\s*>.*$", re.MULTILINE),
    re.compile(r"\"[^\"\n]{0,300}\"|“[^”\n]{0,300}”"),
)


def _claimed_families(session: Session) -> list[tuple[str, tuple[str, ...]]]:
    """(label, tool-substrings) per provenance family claimed in the turn's assistant prose.
    Shared with `has_verifiable_activity` so they can't disagree."""
    prose = "\n".join(m.text for m in session.turn_messages)
    for pattern in _SHOWN_SPANS:
        prose = pattern.sub(" ", prose)
    prose = _REPORTED.sub(" ", prose)        # drop narrated third-party / pre-existing actions
    prose = _REPORTED_STATE.sub(" ", prose)  # drop observed ticket-state (arrow ≠ a self-transition)
    return [(label, tools) for label, pat, tools in _PROVENANCE_FAMILIES if pat.search(prose)]


def tool_call_provenance(session: Session) -> CheckResult:
    """Did the agent's prose claim a tool action that never happened? **Advisory** — it
    reports, it never sinks a run. A claimed family must be backed by *some* tool call of that
    family; no prose, or no recognized claim, is UNSUPPORTED.

    An unbacked claim is UNSUPPORTED, not FAIL, deliberately: the input is prose the agent may
    be *quoting*, so a verdict-bearing FAIL here would be attacker-controllable. The asymmetry
    survives — prose cannot conjure a tool call, so PASS still requires one that happened.

    **A known regression against strategy §7**, which has this check rising in value. The
    narrower first-person rule that would keep the teeth was measured against 6,584 real
    assistant messages: of 41 matches, 4 were first-person governed and one of those was a
    false positive. Agents write tool actions subject-dropped ("Created TYCHOE-11"), so that
    rule keeps ~10% of real claims and none of the web family. Restoring the teeth needs a
    claim whose *author* is known — a declared claim channel, not a better pattern over prose.
    """
    if not session.turn_messages:
        return _r("tool_call_provenance", CheckStatus.UNSUPPORTED, "no assistant prose captured to check")
    claimed = _claimed_families(session)
    if not claimed:
        return _r("tool_call_provenance", CheckStatus.UNSUPPORTED, "no tool-action claims recognized in the turn")
    tools = [e.tool.lower() for e in session.turn_events]
    unbacked = [label for label, subs in claimed if not any(s in t for t in tools for s in subs)]
    if unbacked:
        return _r(
            "tool_call_provenance",
            CheckStatus.UNSUPPORTED,
            f"claimed {', '.join(unbacked)} with no matching tool call this turn — advisory:"
            " prose can be quoted or injected, so this is a hint to confirm, not a verdict",
        )
    backed = ", ".join(label for label, _ in claimed)
    return _r("tool_call_provenance", CheckStatus.PASS, f"claimed actions are backed by tool calls ({backed})")


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
)

_TEST_CHECKS = frozenset({
    "command_execution", "test_freshness", "test_provenance",
    "assertion_weakening", "skip_mock_injection",
})


def run_checks(session: Session) -> list[CheckResult]:
    disabled = set(session.config.disabled_checks)
    if not session.has_tests:
        disabled.update(_TEST_CHECKS)
    return [check(session) for check in CHECKS if check.__name__ not in disabled]


def has_verifiable_activity(session: Session) -> bool:
    """Whether an automatic Stop verdict has meaningful work to report.

    Turn-scoped, else a read-only turn re-reports long-committed work as its own. A claim
    counts: an MCP-only turn has no edits or runners, but its claim is the thing to check.
    """
    return bool(
        session.turn_edits or _runner_events(session.turn_events) or _claimed_families(session)
    )


# --- helpers ----------------------------------------------------------------

def _r(name: str, status: CheckStatus, evidence: str) -> CheckResult:
    return CheckResult(name, status, evidence)


def _scope(session: Session) -> str:
    """What the evidence line calls its scope. No turn boundary means "session"."""
    return "turn" if session.turn_start else "session"


def _runner_events(events) -> list:
    """The test/build runner invocations among ``events`` — pass the scope you mean."""
    return [
        e
        for e in events
        if e.tool in _SHELL_TOOLS and _runner_segment(e.input.get("command") or "") is not None
    ]


def _last_green_run_ts(session: Session) -> float | None:
    # Session-scoped: a run three turns back still covers a source unchanged since. A green
    # following an unresolved red is the narrowed re-run, not the suite.
    reds = _unresolved_reds(session.events, session.commands)
    greens = [
        e.ts
        for e in _runner_events(session.events)
        if _outcome(e, session.commands) is False and not any(r.ts < e.ts for r in reds)
    ]
    return max(greens) if greens else None


# Editing these can't invalidate a green run, and STALE sinks the whole verdict. Narrow on
# purpose — config and lockfiles stay sources; a dependency change can break tests.
_PROSE_SUFFIXES = frozenset({
    ".md", ".rst", ".txt", ".adoc",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf",
})
_PROSE_NAMES = frozenset({"LICENSE", "NOTICE", "AUTHORS", "CODEOWNERS"})


def _is_prose_path(path: str) -> bool:
    """True for a file whose edits can't change what a test run proves."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base in _PROSE_NAMES:
        return True
    dot = base.rfind(".")
    return dot > 0 and base[dot:].lower() in _PROSE_SUFFIXES


def _is_source_path(path: str) -> bool:
    """True for a file a test run actually covers — not a test, not prose."""
    return not _is_test_path(path) and not _is_prose_path(path)


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{p}"
        or p.startswith("tests/")
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
    )


def _is_in_repo(path: str) -> bool:
    """True for a path `_relpath` left repo-relative. Out-of-repo edits stay absolute in
    native *or* POSIX flavor, so both are tested."""
    return not Path(path).is_absolute() and not PurePosixPath(path).is_absolute()


def _ast_check(session: Session, name: str, differ, clean_msg: str) -> CheckResult:
    test_edits = [fe for fe in session.edits if _is_test_path(fe.path)]
    if not test_edits:
        return _r(name, CheckStatus.UNSUPPORTED, "no edited test files to diff")
    # earliest original per path = the file before the session's first edit
    firsts: dict[str, str] = {}
    for fe in sorted(test_edits, key=lambda e: e.ts):
        if fe.original is not None:
            firsts.setdefault(fe.path, fe.original)
    if not firsts:
        # Edited, but no pre-session baseline to diff against: a capability gap, not an
        # all-clear.
        missing = ", ".join(sorted({fe.path for fe in test_edits}))
        return _r(name, CheckStatus.UNSUPPORTED, f"edited test file(s) with no pre-session baseline to diff: {missing}")
    findings = []
    for path, before in firsts.items():
        fs = session.files.get(path)
        after = fs.current_text if fs else None
        findings.extend(f"{path}: {f}" for f in differ(before, after))
    if findings:
        return _r(name, CheckStatus.FAIL, "; ".join(findings))
    return _r(name, CheckStatus.PASS, clean_msg)


def _short(cmd: str, limit: int = 50) -> str:
    cmd = cmd.strip().splitlines()[0] if cmd.strip() else cmd
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"

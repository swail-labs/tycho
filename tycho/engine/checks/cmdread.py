"""Read a shell command: is a test/build runner in there, and how much of the suite did it
run? Pure string work — no I/O, no session.

Every give-up path here returns "no runner found", which reads downstream as no evidence of
a test run. That is the safe direction: the opposite give-up lives in `outcome._status_is_masked`.
"""

from __future__ import annotations

import re
import shlex

from .common import _is_test_path


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


# Flags that make a runner exit 0 without running the suite — read as a pass they fabricate a
# green. Whole tokens only: `-n` is xdist parallelism and `-v` is verbose, neither belongs here.
_DISCOVERY_FLAGS = frozenset({
    "--collect-only", "--co", "--no-run", "--listtests", "--list-tests", "--list",
    "--dry-run", "--version", "-V", "--help", "-h", "--fixtures", "--markers",
})
# `tox -e lint` is a linter. Only an env naming itself a test env counts.


# `tox -e lint` is a linter. Only an env naming itself a test env counts.
_TOX_TEST_ENV = re.compile(r"py[\d.]*$|test|unit|integration", re.IGNORECASE)

# Not Bash-only — a PowerShell/Shell tool runs `pytest` just the same.


# Not Bash-only — a PowerShell/Shell tool runs `pytest` just the same.
_SHELL_TOOLS = frozenset({"Bash", "Shell", "sh", "PowerShell", "powershell", "pwsh"})

# Ephemeral-env wrappers put flags between themselves and the runner, defeating a prefix match.


# Ephemeral-env wrappers put flags between themselves and the runner, defeating a prefix match.
# Only *multi-word* phrases count later in the segment — a phrase is never a `--with` value.
_RUN_WRAPPERS = ("uv run", "uvx", "poetry run", "pdm run", "hatch run", "rye run", "npx", "pnpm dlx", "bunx")


_PHRASE_RUNNERS = tuple(r for r in _TEST_RUNNERS if " " in r)


_BARE_RUNNERS = frozenset(r for r in _TEST_RUNNERS if " " not in r)
# Wrapper flags whose next token installs rather than runs: `uv run --with pytest ruff check`
# is not a test run. Everything else is scanned past — an allowlist of every flag uv might


# Wrapper flags whose next token installs rather than runs: `uv run --with pytest ruff check`
# is not a test run. Everything else is scanned past — an allowlist of every flag uv might
# grow is one we'd always be behind, and being behind it hid `uv run --group test pytest`.
_INSTALL_VALUE_FLAGS = frozenset({
    "--with", "--from", "--with-editable", "--with-requirements", "-p", "--package",
})

# `<python> -m <module>` — the module IS the runner, so `"$PY" -m pytest` stays visible.


# `<python> -m <module>` — the module IS the runner, so `"$PY" -m pytest` stays visible.
_MODULE_RUNNERS = ("pytest", "unittest", "nose2")

# Wrappers carrying the real command inside an argument; see `_unwrap`.


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


# Segment a shell command, so a runner name inside a quoted echo/grep argument doesn't count.
_SEGMENT_SEP = re.compile(r"&&|\|\||[;|\n()]")
# Same separators, captured: `_status_is_masked` needs to know *which* one follows.


# Same separators, captured: `_status_is_masked` needs to know *which* one follows.
_SEGMENT_TOKENS = re.compile(r"(&&|\|\||[;|\n()])")


_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S+\s+)+")

# pytest's verdict is the last line; the slack absorbs the trailing warnings block. Small on


# A redirection is plumbing, not an argument. Left in, `2>&1` reads as a positional — which
# is to say as a test selector — and `pytest -q 2>&1` answers "narrowed" for a run that was
# the whole suite. That is how a green whole-suite run failed to supersede an earlier red on
# Tycho's own repo. `2>&1` and `>out.txt` carry their target; a bare `>` takes the next token.
_REDIRECT = re.compile(r"^\d*(?:>>|[<>])")
_BARE_REDIRECT = re.compile(r"^\d*(?:>>|[<>])$")


def _strip_redirects(parts: list[str]) -> list[str]:
    kept, skip = [], False
    for part in parts:
        if skip:
            skip = False
            continue
        if _REDIRECT.match(part):
            skip = bool(_BARE_REDIRECT.match(part))  # the filename is the next token
            continue
        kept.append(part)
    return kept


def _normalize_segment(segment: str) -> str:
    """Strip env prefixes, redirections and the leading path, so `.venv/bin/python -m pytest`
    reads as `python -m pytest`."""
    segment = _ENV_PREFIX.sub("", segment.strip())
    try:
        parts = _strip_redirects(shlex.split(segment))
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
    if _runs_a_test_file(segment):
        return True
    return _runner_span(segment) is not None


# Interpreters a suite is routinely handed to directly, with no runner binary in between.
_DIRECT_INTERPRETERS = frozenset({"ruby", "java", "python", "python3", "node", "perl", "php"})


def _runs_a_test_file(segment: str) -> bool:
    """`ruby test_slug.rb`, `java SlugTest.java` — an interpreter pointed straight at a test
    file.

    Minitest's documented idiom has no runner binary at all, and neither does single-file
    Java. Without this an entirely honest Ruby turn came back INDETERMINATE and took
    `test_freshness` and `test_provenance` down with it ("no passing test run to check
    against") — three checks disarmed because the vocabulary knew `rake test` and `rspec` but
    not the plainest way to run the suite.

    Gated on `_is_test_path`, not on the interpreter alone: `ruby build.rb` is a build script,
    and reading it as a green test run is exactly the fabricated pass this codebase never
    issues.
    """
    tokens = segment.split()
    if len(tokens) < 2 or tokens[0] not in _DIRECT_INTERPRETERS:
        return False
    return any(_is_test_path(t) for t in tokens[1:] if not t.startswith("-"))


def _phrase_end(tokens: list[str], phrase: list[str]) -> int:
    """Index just past `phrase` in `tokens`, allowing the tool's own flags between its words;
    0 when it isn't there.

    `mvn -q test` is a test run and an exact prefix match said it wasn't — a whole honest Maven
    turn came back "no test/build command ran this turn", which disarms the three checks that
    need a passing run. Same shape in `gradle --offline test` and `npm run --silent test`.

    Only `-`-prefixed tokens are skipped, and the first token that isn't a flag has to be the
    next word of the phrase. That is what keeps `cargo build --features test` out: the first
    non-flag after `cargo` is `build`, so the match stops there rather than walking on to find
    a `test` that is a flag's value, not a subcommand.

    A flag that takes its value as the next token carries that token with it, or `uv run --with
    pytest ruff check` matches `uv run pytest` — the install argument read as the command, which
    is the same trap the wrapper scan below already guards.
    """
    i = 0
    for word in phrase:
        while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != word:
            if tokens[i] in _INSTALL_VALUE_FLAGS:
                i += 1
            i += 1
        if i >= len(tokens) or tokens[i] != word:
            return 0
        i += 1
    return i


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
        end = _phrase_end(tokens, runner.split())
        if end:
            longest = max(longest, end)
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
            # Redirects out too, or `tycho exec -- pytest 2>&1` never matches the argv the
            # exec log recorded, and the strongest evidence Tycho has goes unused.
            parts = _strip_redirects(shlex.split(segment))
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


def _covers(green: str | None, red: str | None) -> bool:
    """Did `green` run at least everything `red` did? Both are normalized runner segments.

    The relation is containment of what each run *selected*, and "the whole suite" is just the
    case where green selected everything. Asking only whether green was the whole suite made
    `pytest tests/` fail to supersede `pytest tests/test_one.py` — a run that did strictly more
    work, reported as "a different command, so it can't stand in for it". That is the
    cried-wolf failure: a red nobody can discharge without starting a new session.

    It still only ever loosens where containment is *provable*. An unreadable command, an
    opaque selector (`-k`, `--lf`), a red that ran the whole suite, or a different runner
    family all supersede nothing — the honest answer, not a fallback.
    """
    if green is None or red is None:
        return False
    if green == red:
        return True
    # Different runners are different suites: a green `pytest` says nothing about a red `npm test`.
    if _runner_family(green) is None or _runner_family(green) != _runner_family(red):
        return False
    picked = _selection(green)
    if picked is None or picked is _OPAQUE:
        return False
    if not picked:
        return True  # green selected nothing in particular, which is everything
    covered = _selection(red)
    if covered is None or covered is _OPAQUE or not covered:
        return False  # unreadable, opaque, or red *was* the whole suite — the narrowed-rerun lie
    return all(any(_target_covers(p, c) for p in picked) for c in covered)


def _target_covers(outer: str, inner: str) -> bool:
    """Does running `outer` run `inner` too? Directory over file, file over node id.

    Separator-anchored, never a bare prefix: `tests/test_one.py` must not read as covering
    `tests/test_one_helpers.py`.
    """
    outer, inner = _norm_target(outer), _norm_target(inner)
    if outer == inner:
        return True
    if outer.endswith("/..."):  # go's recursive form
        outer = outer[:-4]
    else:
        outer = outer.rstrip("/")
    return any(inner.startswith(outer + sep) for sep in ("/", "::"))


def _norm_target(target: str) -> str:
    return target[2:] if target.startswith("./") else target


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

# Options taking no value, so the bare word after one is a test target rather than its value.
# The complement of `_VALUE_OPTIONS`, and load-bearing for the same reason: without `-q` here,
# `pytest -q tests/` is "an unmodelled option followed by an ambiguous word" and the whole
# coverage relation declines to read the single most common way people run pytest.
_FLAG_OPTIONS = {
    "pytest": frozenset({"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--quiet",
                         "--verbose", "--exitfirst", "--no-header", "--no-summary",
                         "--showlocals", "--strict-markers", "--strict-config",
                         "--disable-warnings", "--cache-clear", "--full-trace", "-ra", "-rA"}),
    "unittest": frozenset({"-v", "-q", "-f", "-b", "--verbose", "--quiet", "--failfast",
                           "--buffer", "--locals"}),
    "go": frozenset({"-v", "-race", "-cover", "-short", "-failfast", "-json", "-bench"}),
    "cargo": frozenset({"-v", "-q", "--verbose", "--quiet", "--release", "--all", "--workspace",
                        "--all-features", "--no-default-features", "--all-targets",
                        "--no-fail-fast", "--locked", "--offline"}),
    "tox": frozenset({"-v", "-q", "-r", "--recreate", "--notest", "--parallel-no-spinner"}),
    "js": frozenset({"--ci", "--coverage", "--silent", "--verbose", "--runInBand", "--bail",
                     "--passWithNoTests", "--watchAll", "-u", "--updateSnapshot", "--run"}),
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


# Narrowed by something that is not a path: `-k auth` runs an unknown subset, so nothing but an
# identical command or a whole-suite run can be shown to have covered it.
_OPAQUE = object()


def _selection(segment: str) -> frozenset[str] | object | None:
    """What this run selected: a set of targets, empty for the whole suite, `_OPAQUE` when
    narrowed by a non-path selector, None when the arguments can't be read.

    None is an answer, not a failure mode: an unrecognized option followed by a bare word could
    be that option's value or a test selector. Callers treat None as "does not supersede".
    That rule is why `_FLAG_OPTIONS` has to exist — without the options we *know* take no
    value, `pytest -q tests/` is unreadable, and unreadable is the most common shape there is.
    """
    family = _runner_family(segment)
    span = _runner_span(segment)
    if family is None or span is None:
        return None
    value_opts = _VALUE_OPTIONS[family]
    flag_opts = _FLAG_OPTIONS[family]
    narrowing = _NARROWING_OPTIONS[family]
    whole = _WHOLE_SUITE_POSITIONALS[family]

    targets: set[str] = set()
    tokens = segment.split()[span[1]:]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":  # `npm test -- <runner args>`
            i += 1
            continue
        if not token.startswith("-"):
            if token not in whole:
                targets.add(token)  # a test path or node id
            i += 1
            continue
        name = token.split("=", 1)[0]
        if name in narrowing:
            return _OPAQUE
        if "=" in token or name in flag_opts:  # self-contained, never consumes the next token
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
    return frozenset(targets)


def _selects_whole_suite(segment: str) -> bool | None:
    """True = the whole suite, False = narrowed, None = the arguments can't be read."""
    picked = _selection(segment)
    if picked is None:
        return None
    return picked is not _OPAQUE and not picked

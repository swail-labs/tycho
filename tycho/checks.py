"""The deterministic checks. Each is a pure function of the Session snapshot.

A check returns exactly one CheckResult. When a check doesn't apply (no relevant
input) it returns UNSUPPORTED with a reason — never a false FAIL. `run_checks`
drives the registry and honours `config.disabled_checks`.
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
)

# The shell tools an agent runs commands through. Runner detection isn't Bash-specific:
# a PowerShell/pwsh/Shell tool runs `pytest` just the same, and a Bash-only filter made
# those runs — and every check that depends on them — invisible (TYCHO-27). Cursor already
# maps Shell→Bash in its reader; this covers tools/harnesses that don't normalize.
_SHELL_TOOLS = frozenset({"Bash", "Shell", "sh", "PowerShell", "powershell", "pwsh"})

# Ephemeral-env wrappers put their own flags between the wrapper and the real runner:
# `uv run --python 3.12 --with pytest python -m pytest -q`. The plain runner match misses
# that, so for a wrapper we also look for a *multi-word* runner phrase later in the segment
# (TYCHO-27). Multi-word phrases like `python -m pytest` are never a `--with` value, which
# keeps this from firing on `uv run --with pytest ruff check`.
_RUN_WRAPPERS = ("uv run", "uvx", "poetry run", "pdm run", "hatch run", "rye run", "npx", "pnpm dlx", "bunx")
_PHRASE_RUNNERS = tuple(r for r in _TEST_RUNNERS if " " in r)

# `<python> -m <module>` — the module IS the runner, so the leading executable can be
# anything, including a shell variable (`"$PY" -m pytest`) or path we can't resolve
# statically. Recognising the module directly is what makes a variable-indirected
# interpreter visible instead of counted as "no test ran" (TYCHO-88).
_MODULE_RUNNERS = ("pytest", "unittest", "nose2")

# Wrappers that carry the real command *inside* an argument, so the runner is invisible
# until we descend into it (TYCHO-89). `_unwrap` peels these; the shapes it knows are
# `<w> ... -- <cmd>` (wsl/env), `<shell> -c '<cmd>'`, `ssh <host> <cmd>`, and our own
# `tycho run` escape hatch (TYCHO-90). `wsl.exe`/`bash.exe` reduce to the bare name first.
_DASHDASH_WRAPPERS = ("wsl", "env")
_C_SHELLS = ("bash", "sh", "zsh", "dash", "ash")
_EXE_SUFFIX = re.compile(r"\.(?:exe|bat|cmd|ps1)$", re.IGNORECASE)


def _looks_like_interpreter(tok: str) -> bool:
    """True for a token that could be a Python interpreter — a real name (`python3.12`,
    the `py` launcher) or an unresolved shell/Windows variable (`$PY`, `%PY%`, `${PY}`)
    whose value we can't see. Keeps the `-m pytest` rule off `echo -m pytest` (TYCHO-88)."""
    if tok[:1] in "$%{":
        return True
    name = tok.lower()
    return name == "py" or bool(re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", name))

# Split a shell command into segments so a runner name buried in a quoted
# echo/grep argument doesn't count as "the tests ran".
_SEGMENT_SEP = re.compile(r"&&|\|\||[;|\n()]")
# The same separators, kept as capture groups: *which* separator follows the runner is the
# whole question in `_status_is_masked`, so unlike `_SEGMENT_SEP` we can't discard them.
_SEGMENT_TOKENS = re.compile(r"(&&|\|\||[;|\n()])")
_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S+\s+)+")

# How much of a runner's output to read back for its summary. pytest's verdict is the last
# line; the slack absorbs the trailing warnings block and blank lines that follow it under
# `-q`. Deliberately small: the further up the blob we look, the more we're reading the run
# rather than its conclusion.
_SUMMARY_TAIL_LINES = 12


def _normalize_segment(segment: str) -> str:
    """Strip env prefixes and any leading path from the executable so
    `.venv/bin/python -m pytest` reads as the same runner as bare `pytest`."""
    segment = _ENV_PREFIX.sub("", segment.strip())
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = []
    if parts:
        exe = parts[0].rsplit("/", 1)[-1]
        # Windows interpreters carry a suffix (`python.exe`); strip it so a venv
        # runner reads the same as its POSIX `python` bare name (TYCHO-52).
        exe = re.sub(r"\.(?:exe|bat|cmd|ps1)$", "", exe, flags=re.IGNORECASE)
        segment = " ".join([exe, *parts[1:]])
    return segment


def _is_runner(segment: str) -> bool:
    """True if this already-normalized segment invokes a test/build runner."""
    java_runner = segment.startswith("java ") and ("junit" in segment.lower() or "testng" in segment.lower())
    if java_runner or any(segment == r or segment.startswith(f"{r} ") for r in _TEST_RUNNERS):
        return True
    # `<interpreter> -m pytest|unittest` regardless of the interpreter token, so a variable
    # or unresolved-path exe still counts (TYCHO-88). Guarded so `echo -m pytest` doesn't.
    parts = segment.split()
    if (
        len(parts) >= 3
        and parts[1] == "-m"
        and parts[2] in _MODULE_RUNNERS
        and _looks_like_interpreter(parts[0])
    ):
        return True
    # An ephemeral-env wrapper (`uv run …`) with a multi-word runner phrase further along
    # counts as a runner even though the flags in between defeat the plain prefix match.
    if any(segment == w or segment.startswith(f"{w} ") for w in _RUN_WRAPPERS):
        padded = f" {segment} "
        return any(f" {r} " in padded for r in _PHRASE_RUNNERS)
    return False


def _unwrap(segment: str) -> str | None:
    """If `segment` wraps the real command inside an argument, return that inner command
    (quotes intact) to recurse into; else None. Runs on the RAW segment, not a normalized
    one, so a quoted `-c '<cmd>'` stays a single token instead of splitting apart (TYCHO-89).

    Peels one layer; `_runner_segment` recurses, so `wsl -- bash -c 'pytest'` unwinds
    `wsl -- …` → `bash -c 'pytest'` → `pytest`.
    """
    try:
        parts = shlex.split(segment)
    except ValueError:
        return None
    if not parts:
        return None
    head = _EXE_SUFFIX.sub("", parts[0].rsplit("/", 1)[-1]).lower()

    # Our opt-in escape hatches: `tycho run|exec [--] <cmd>` both exec <cmd> and forward its
    # real exit code, so the runner inside is visible here and trustworthy at runtime
    # (TYCHO-90). `exec` additionally logs what it saw — see `_exec_run_for`.
    if head == "tycho" and len(parts) >= 2 and parts[1] in ("run", "exec"):
        rest = parts[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return shlex.join(rest) if rest else None

    # `<wrapper> ... -- <cmd...>` : the command is everything past the first `--`
    # (`wsl.exe -d Ubuntu -- <cmd>`, `env FOO=bar -- <cmd>`).
    if head in _DASHDASH_WRAPPERS and "--" in parts:
        rest = parts[parts.index("--") + 1 :]
        return shlex.join(rest) if rest else None

    # `<shell> ... -c '<cmd>'` : the command is the single arg after a -c-family flag.
    if head in _C_SHELLS:
        for i in range(1, len(parts) - 1):
            flag = parts[i]
            if flag.startswith("-") and "c" in flag.lstrip("-").lower():
                return parts[i + 1]

    # `ssh <host> <cmd...>` : the remote command trails the host. Conservative — only the
    # no-option form, so an option value is never mistaken for the host.
    if head == "ssh" and len(parts) >= 3 and not parts[1].startswith("-"):
        return shlex.join(parts[2:])

    # `docker run <img> <cmd>` is not unwrapped — unlike the shells above it needs the
    # image and its flags skipped first; add when a docker test workflow needs it (TYCHO-89).
    return None


def _runner_segment(cmd: str) -> str | None:
    """The command segment that is a test/build runner, or None. Splitting on shell
    separators keeps a runner name quoted inside an echo/grep from counting; unwrapping
    descends into wrappers (wsl/ssh/`bash -c`/`tycho run`) that hide it (TYCHO-89/90)."""
    for segment in _SEGMENT_SEP.split(cmd):
        norm = _normalize_segment(segment)
        if _is_runner(norm):
            return norm
        inner = _unwrap(segment)
        if inner is not None:
            found = _runner_segment(inner)
            if found is not None:
                return found
    return None


def _exec_argv(cmd: str) -> list[str] | None:
    """The argv `tycho exec` was given inside `cmd`, or None if it wasn't used.

    Recurses through the same wrappers `_unwrap` knows, so `bash -c 'tycho exec -- pytest'`
    still yields `["pytest"]`. This is the join between the two evidence streams: the
    transcript says *what the turn ran*, the exec log says *what Tycho observed*, and the
    inner argv is the only thing both of them independently know.
    """
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
            found = _exec_argv(inner)
            if found is not None:
                return found
    return None


def _exec_run_for(event, commands) -> "CommandRun | None":  # noqa: F821 — model.CommandRun
    """The `tycho exec` evidence for this transcript event, or None.

    Matched on the inner argv, not on timestamps: harness clocks vary (Cursor stamps
    nothing at all), while the argv is written independently by both sides and has to agree.
    `commands` is already floored to this turn/session by `verify.gather`, so an old run of
    the same command is not in the running — that is where staleness is prevented, and it is
    prevented by construction rather than by a heuristic here.

    The **latest** match wins, the same rule `command_execution` already applies to events:
    when a turn ran the suite twice, the claim rests on the last one.

    ponytail: an exact argv match. A command whose *string* was altered by redaction
    (`TOKEN=x pytest`) simply won't match, and the check falls back to its pre-exec
    behaviour — losing evidence, never inventing it, which is the safe side of that trade.
    """
    if not commands:
        return None
    argv = _exec_argv(event.input.get("command") or "")
    if not argv:
        return None
    best = None
    for run in commands:
        try:
            if shlex.split(run.cmd) == argv and (best is None or run.ended_at >= best.ended_at):
                best = run
        except ValueError:
            continue
    return best


def _status_is_masked(cmd: str) -> bool:
    """True when the exit status the harness recorded is *not* the runner's own.

    The shell reports one status for the whole command: the last thing it ran. So a
    runner's failure only reaches us if nothing overwrote it, and there are three ways it
    gets overwritten (TYCHO-26/31/61) — all of them real commands agents write:

        pytest | tail -1      the pipeline's status is tail's (no pipefail here)
        pytest; echo done     the status is echo's; `;` discards what came before
        pytest || true        `||` swallows the failure by construction

    `&&` is the one shape that is *safe* and must not be flagged: `pytest && echo ok`
    fails the whole command when pytest fails, so a recorded success really does mean the
    runner succeeded. Flagging it would cost us the most common honest invocation there is.

    A runner reached through a wrapper (`wsl -- bash -c 'pytest | tail'`) is masked when its
    *inner* command is — the wrapper can only forward as clean a status as it was given — so
    the wrapper's inner command is checked too (TYCHO-89).

    Trusting a masked status is how a red suite gets reported VERIFIED, which is the
    single worst thing this program can do — so when in doubt, say masked and let
    `_outcome` fall back to what the runner said in its own output.
    """
    parts = _SEGMENT_TOKENS.split(cmd)  # [segment, sep, segment, sep, ..., segment]
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not _is_runner(_normalize_segment(seg)):
            inner = _unwrap(seg)
            if inner is None or _runner_segment(inner) is None:
                continue  # not the runner segment, wrapped or otherwise — keep looking
            if _status_is_masked(inner):
                return True  # a masking operator inside the wrapper hides the runner's status
        # Walk what follows this runner (or its wrapper): the first separator that redirects,
        # swallows, or supersedes the status is the one that masks it.
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

    Only the tail, and that is the point. Claude Code caps `toolUseResult.stdout` at 30k
    characters and keeps the *head* (verified against 2356 real payloads — see
    docs/harness-support.md), while pytest prints its summary *last*. So on a long run the
    verdict is exactly what got cut, and reading the tail means a truncated capture finds
    no summary and honestly reports nothing — where scanning the whole blob could match a
    stray "5 passed" from the middle of a run that ended red.
    """
    result = event.result or {}
    text = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr")).strip()
    return "\n".join(text.splitlines()[-_SUMMARY_TAIL_LINES:]) if text else ""


def _outcome(event, commands=()) -> bool | None:
    """Did this runner invocation fail? True = failed, False = passed, None = can't tell.

    One predicate for every caller, so `command_execution` and `_last_green_run_ts` can
    never disagree about what "green" means — a split brain there is how a run gets
    reported green by one check and stale-against-nothing by the other.

    **The evidence ladder, strongest first:**

    1. *A status Tycho captured itself* (`tycho exec`). Tycho was the parent process and
       read `wait()`. There is no shell in between to mask it, no harness in between to
       drop or truncate it, and no way to run the command without producing it. Strictly
       stronger than anything the transcript can offer, which is the entire point of §9.6.
    2. *The transcript's exit code*, when it is genuinely the runner's — i.e. nothing in
       the command line masked it (`_status_is_masked`). Real, but the harness had to
       choose to keep it, and three of the four often don't.
    3. *The runner's own summary line*, read back out of whatever output survived. Still
       evidence — the runner reporting on itself — but the weakest kind, and the one a
       30k head-truncation silently destroys. Callers say so in their output.

    **When 1 and 2 disagree**, failure wins: Tycho's status decides whether the *runner*
    failed, and a trustworthy transcript status may add a failure it can never remove.
    Asymmetric on purpose. `tycho exec -- pytest && ./deploy.sh` can fail for a reason
    Tycho's capture cannot see, and reading that as "the runner passed, all good" would be
    a fabricated green — the one failure this program must never have. The reverse (the
    transcript is green because a `;` swallowed the status, Tycho saw exit 1) is exactly the
    lie `tycho exec` exists to catch, and rung 1 catches it.
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
    # Say where the verdict came from. A status recovered from output is real evidence but
    # weaker than an exit code, and a reader who can't tell which they got can't judge it.
    if run is not None:
        via = f" (Tycho ran it — exit {run.exit_code})"
    else:
        via = " (read from its output — exit status masked by the shell)" if masked else ""
    if outcome:
        return _r("command_execution", CheckStatus.FAIL, f"`{cmd}` ran but reported an error{via}")
    return _r("command_execution", CheckStatus.PASS, f"`{cmd}` ran without error{via}")


def test_freshness(session: Session) -> CheckResult:
    green_ts = _last_green_run_ts(session)
    if green_ts is None:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no passing test run to check against")
    source_edits = [fe for fe in session.edits if _is_source_path(fe.path)]
    if not source_edits:
        return _r("test_freshness", CheckStatus.UNSUPPORTED, "no source edits to check against the run")
    stale = []
    for fe in source_edits:
        fs = session.files.get(fe.path)
        if fs and fs.mtime is not None and fs.mtime > green_ts:
            stale.append((fe.path, fs.mtime))
    if stale:
        path, mt = max(stale, key=lambda x: x[1])
        # Session-scoped by design (TYCHO-23): staleness is a fact about the tree *now*, so an
        # uncovered source from an earlier turn still counts. But don't *imply* a this-turn edit:
        # reserve the "edited Ns after" phrasing for turn edits, and word an earlier-turn
        # staleness as the live condition it is.
        if path in {fe.path for fe in session.turn_edits}:
            evidence = f"{path} edited {int(mt - green_ts)}s after the last passing test run"
        else:
            evidence = f"{path} still uncovered since the last passing run (last edited in an earlier turn)"
        return _r("test_freshness", CheckStatus.STALE, evidence)
    return _r("test_freshness", CheckStatus.PASS, "sources unchanged since the last passing run")


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
    # Only in-repo paths are git's to speak to. `_relpath` keeps an out-of-repo edit
    # absolute; judging one against this repo's diff would pass on file_state's evidence
    # (the file exists on disk), not git's, and report it "reconciled" (TYCHO-45).
    in_repo = {p for p in edited if _is_in_repo(p)}
    outside = edited - in_repo
    if not in_repo:
        return _r(
            "git_state",
            CheckStatus.UNSUPPORTED,
            f"{len(outside)} path(s) edited outside the repo — nothing here for git to reconcile",
        )
    changed = set(session.git.changed_paths)
    # light git check — a phantom is an edit that's neither in the working diff
    # nor on disk. Richer commit-claim verification arrives with manual --claim/--since.
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
        # Point the reader at the fix — a bare "zero-config" is a dead end in the Stop output
        # (TYCHO-74). `tycho scope add` is the concrete action; `tycho scope`/`tycho help`
        # cover the rest, so no dedicated scope-help command is needed.
        return _r("scope_drift", CheckStatus.UNSUPPORTED,
                  "no [scope] set — run `tycho scope add '<glob>'` to bound edits (zero-config)")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "scope_drift",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to check scope against",
        )
    # In scope iff it matches an include glob AND no exclude glob — exclude wins (TYCHO-78).
    def _in_scope(p: str) -> bool:
        return any(fnmatchcase(p, g) for g in globs) and not any(fnmatchcase(p, g) for g in excludes)

    outside = [p for p in sorted(edited) if not _in_scope(p)]
    if outside:
        return _r("scope_drift", CheckStatus.FAIL, f"edits outside stated scope: {', '.join(outside)}")
    within = f"all edits within scope {list(globs)}"
    return _r("scope_drift", CheckStatus.PASS, f"{within} (excluding {list(excludes)})" if excludes else within)


# tool_call_provenance families (TYCHO-91). Each is (label, claim pattern, tool-name
# substrings). BROAD by design: a claim of a family requires *some* tool call of that family
# in the turn — not a content match, which no generalizable tool schema supports and which
# would risk a false FAIL (deferred to tycho-web/TYCHO-85 if ever needed). The claim patterns
# are first-person, past-tense assertions of a *completed* action (never future/hypothetical),
# anchored on unambiguous cues (a web verb, or a ticket KEY next to a ticket verb) so an honest
# turn is never failed; a claim we can't classify is simply not counted. This table is the
# calibration knob — widen coverage by adding rows.
_PROV_WEB = re.compile(
    r"\b(?:searched (?:the web|online)|web[- ]?searched|googled|"
    r"browsed to|fetched the (?:page|url|web ?site|site))\b",
    re.IGNORECASE,
)
# An issue-tracker action is claimed when a ticket anchor (a KEY like TYCHO-95, or "Jira
# ticket/issue/card") sits within a short window of an action cue — in *either* order, since
# real prose says both "moved TYCHO-29 to In Progress" and "TYCHO-29 moved to Done" (TYCHO-95).
# Two cue kinds, both conservative enough to never false-FAIL an honest/future turn:
#   - a *past-tense* action verb (a future "I'll move TYCHO-40" uses base "move", never "moved");
#   - a *two-status arrow* ("Hold → In Review"), which only ever reports a transition that
#     happened — a plan is written "I'll move it to In Review", never "Hold → In Review".
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

# A cue whose subject is a third party, or that reports a pre-existing state, is the agent
# *narrating* — not claiming it just acted. "Dan closed TYCHO-97", "the operator moved it",
# "TYCHO-30 was already closed", "Dan searched the web" all describe someone else's or an
# already-true action, and firing on them is a false FAIL (the exact live miss: reporting that
# a ticket the agent looked up is already Done — follow-up to TYCHO-91/95). Neutralize these
# clauses before matching, so provenance judges only the agent's own claims — subject-dropped
# ("TYCHO-30 closed") and first-person ("I closed") survive untouched. Recall loss (a genuine
# fabrication sitting right after a name is missed) is accepted over any false FAIL, exactly as
# the rest of the check is tuned. The name branch is case-sensitive so a lowercase word ("and
# moved") is never mistaken for a subject; the verbs stay case-insensitive.
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

# An *observed* ticket state — "TYCHO-30 already sits at In Review → Done", "it's now at Hold →
# Done", "the board shows In Review → Done" — reports where a ticket already is, not a transition
# the agent performed. The status-arrow branch of _ISSUE_CUE otherwise reads any arrow as a
# self-made transition and false-FAILs the observation (the live miss: narrating board state Tycho
# never changed). Neutralize the stative clause *through its arrow* so nothing downstream fires; a
# real self-claim keeps an action verb ("moved") or completion noun ("Round-trip complete on … :")
# and is untouched. Recall loss on a purely verbless observed arrow is accepted, as the rest of the
# check is tuned. The subject is a KEY, "it", or "the ticket/card/board/issue" — the things prose
# points a stative verb at; the verb list is case-insensitive, the anchor stays case-sensitive so a
# lowercase word is never mistaken for a KEY.
_STATE_VERB = r"(?i:sits?|stands?|shows?|reads?|remains?|stays?|is|are|'s|was|were)"
_REPORTED_STATE = re.compile(
    rf"(?:{_ISSUE_ANCHOR}|\bit\b|\b[Tt]he\s+(?:ticket|card|board|issue))\s+"
    rf"(?i:already |just |now |currently |still )*{_STATE_VERB}\b"
    rf"[^.\n]*?{_ISSUE_STATUS}\s*(?:→|-+>|↔)\s*{_ISSUE_STATUS}"
)


def _claimed_families(session: Session) -> list[tuple[str, tuple[str, ...]]]:
    """(label, tool-substrings) for each provenance family whose claim appears in the turn's
    assistant prose. Shared by the check and `has_verifiable_activity` so they can't disagree."""
    prose = "\n".join(m.text for m in session.turn_messages)
    prose = _REPORTED.sub(" ", prose)        # drop narrated third-party / pre-existing actions
    prose = _REPORTED_STATE.sub(" ", prose)  # drop observed ticket-state (arrow ≠ a self-transition)
    return [(label, tools) for label, pat, tools in _PROVENANCE_FAMILIES if pat.search(prose)]


def tool_call_provenance(session: Session) -> CheckResult:
    """Did the agent's prose claim a tool action that never happened? (TYCHO-91)

    Deterministic and broad, never an LLM judge: a claim of a family (web search, issue-tracker
    action) must be backed by *some* tool call of that family in the turn. An unbacked claim is
    a fabricated action — the thing a user can't see in the chat — so it FAILs. No prose to read
    (a harness we don't mine yet) or no recognized claim is UNSUPPORTED, never a false FAIL.
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
            CheckStatus.FAIL,
            f"claimed {', '.join(unbacked)} with no matching tool call this turn",
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
    """Whether an automatic Stop verdict would have meaningful work to report.

    Turn-scoped, and that is the whole of TYCHO-17: session-scoped, a read-only turn
    in a session that edited anything earlier still looked like "activity", so every
    later Stop re-reported long-committed work as though it were this turn's. A turn
    that did nothing verifiable earns silence, not a stale green tick.

    A tool-action *claim* in the prose counts too (TYCHO-91): an MCP-only turn ("I created
    the ticket") has no edits or runners, but its claim is exactly what tool_call_provenance
    exists to check — so the hook must speak rather than stay silent on it.
    """
    return bool(
        session.turn_edits or _runner_events(session.turn_events) or _claimed_families(session)
    )


# --- helpers ----------------------------------------------------------------

def _r(name: str, status: CheckStatus, evidence: str) -> CheckResult:
    return CheckResult(name, status, evidence)


def _scope(session: Session) -> str:
    """What the turn-scoped checks should *call* their scope in the evidence line.

    A 0.0 boundary means nothing narrowed the view — `tycho verify` auditing a whole
    session, or a harness that can't mark turns — so the honest word is "session".
    Saying "turn" there would re-tell the exact lie TYCHO-17 is about.
    """
    return "turn" if session.turn_start else "session"


def _runner_events(events) -> list:
    """The test/build runner invocations among ``events`` — pass the scope you mean."""
    return [
        e
        for e in events
        if e.tool in _SHELL_TOOLS and _runner_segment(e.input.get("command") or "") is not None
    ]


def _last_green_run_ts(session: Session) -> float | None:
    # Session-scoped on purpose: a run three turns back still covers a source that
    # hasn't changed since, and test_freshness/test_provenance are exactly the checks
    # whose job is to reason across turns.
    greens = [e.ts for e in _runner_events(session.events) if _outcome(e, session.commands) is False]
    return max(greens) if greens else None


# Prose and pictures. A green test run can't be invalidated by editing them, so
# test_freshness must not count them as sources — a doc edit after a passing run is
# not staleness, and STALE sinks the whole verdict. Kept deliberately narrow: config
# and lockfiles stay sources, because changing a dependency really can break tests.
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
    """True for a path `_relpath` left repo-relative. An out-of-repo edit is kept
    absolute (native *or* POSIX flavor — the host can't judge the other), so those are
    not ours to reconcile against this repo's git (TYCHO-45)."""
    return not Path(path).is_absolute() and not PurePosixPath(path).is_absolute()


def _ast_check(session: Session, name: str, differ, clean_msg: str) -> CheckResult:
    test_edits = [fe for fe in session.edits if _is_test_path(fe.path)]
    if not test_edits:
        return _r(name, CheckStatus.UNSUPPORTED, "no edited test files to diff")
    # earliest original per test path = the file's state before the session's first edit
    firsts: dict[str, str] = {}
    for fe in sorted(test_edits, key=lambda e: e.ts):
        if fe.original is not None:
            firsts.setdefault(fe.path, fe.original)
    if not firsts:
        # Test files WERE edited but no pre-session baseline is available — the harness
        # omitted `originalFile` and git couldn't supply it (untracked). A capability gap,
        # not an all-clear: say so distinctly so `•` doesn't read as "tests untouched" (TYCHO-32).
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

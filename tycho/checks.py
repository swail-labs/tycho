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
)

# Not Bash-only: a PowerShell/Shell tool runs `pytest` just the same, and filtering them out
# makes those runs invisible to every check.
_SHELL_TOOLS = frozenset({"Bash", "Shell", "sh", "PowerShell", "powershell", "pwsh"})

# Ephemeral-env wrappers put flags between themselves and the runner (`uv run --with pytest
# python -m pytest`), defeating a prefix match. Only *multi-word* phrases count later in the
# segment: a phrase is never a `--with` value, so `uv run --with pytest ruff check` can't fire.
_RUN_WRAPPERS = ("uv run", "uvx", "poetry run", "pdm run", "hatch run", "rye run", "npx", "pnpm dlx", "bunx")
_PHRASE_RUNNERS = tuple(r for r in _TEST_RUNNERS if " " in r)
_BARE_RUNNERS = frozenset(r for r in _TEST_RUNNERS if " " not in r)
# Wrapper flags whose *next* token is a value, not the command. `--with pytest` installs
# pytest; it does not run it. Anything else starting with `-` is treated as a boolean flag,
# so an unknown one never swallows the command that follows it.
_WRAPPER_VALUE_FLAGS = frozenset({
    "--with", "--from", "--python", "-p", "--index", "--index-url", "--extra-index-url",
    "--constraint", "--override", "--package", "--project", "--directory", "--refresh-package",
})

# `<python> -m <module>` — the module IS the runner, so a variable-indirected interpreter
# (`"$PY" -m pytest`) is still visible instead of counting as "no test ran".
_MODULE_RUNNERS = ("pytest", "unittest", "nose2")

# Wrappers carrying the real command *inside* an argument, invisible until `_unwrap` descends:
# `<w> ... -- <cmd>` (wsl/env), `<shell> -c '<cmd>'`, `ssh <host> <cmd>`, `tycho run`.
_DASHDASH_WRAPPERS = ("wsl", "env")
_C_SHELLS = ("bash", "sh", "zsh", "dash", "ash")
_EXE_SUFFIX = re.compile(r"\.(?:exe|bat|cmd|ps1)$", re.IGNORECASE)


def _looks_like_interpreter(tok: str) -> bool:
    """True for a token that could be a Python interpreter — a real name (`python3.12`, `py`)
    or an unresolved variable (`$PY`, `%PY%`). Keeps the `-m pytest` rule off `echo -m pytest`."""
    if tok[:1] in "$%{":
        return True
    name = tok.lower()
    return name == "py" or bool(re.fullmatch(r"python[0-9.]*|pypy[0-9.]*", name))

# Split a shell command into segments so a runner name buried in a quoted
# echo/grep argument doesn't count as "the tests ran".
_SEGMENT_SEP = re.compile(r"&&|\|\||[;|\n()]")
# Same separators as capture groups: `_status_is_masked` needs to know *which* one follows.
_SEGMENT_TOKENS = re.compile(r"(&&|\|\||[;|\n()])")
_ENV_PREFIX = re.compile(r"^(?:\s*\w+=\S+\s+)+")

# How much output to read back for the summary. pytest's verdict is the last line; the slack
# absorbs the trailing warnings block. Small on purpose — further up we'd read the run, not
# its conclusion.
_SUMMARY_TAIL_LINES = 12


def _normalize_segment(segment: str) -> str:
    """Strip env prefixes and the executable's leading path, so `.venv/bin/python -m pytest`
    reads as the same runner as bare `pytest`."""
    segment = _ENV_PREFIX.sub("", segment.strip())
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = []
    if parts:
        exe = parts[0].rsplit("/", 1)[-1]
        # Strip the Windows suffix so `python.exe` reads as POSIX `python`.
        exe = re.sub(r"\.(?:exe|bat|cmd|ps1)$", "", exe, flags=re.IGNORECASE)
        segment = " ".join([exe, *parts[1:]])
    return segment


def _is_runner(segment: str) -> bool:
    """True if this already-normalized segment invokes a test/build runner."""
    java_runner = segment.startswith("java ") and ("junit" in segment.lower() or "testng" in segment.lower())
    if java_runner or any(segment == r or segment.startswith(f"{r} ") for r in _TEST_RUNNERS):
        return True
    # `<interpreter> -m pytest`, guarded so `echo -m pytest` doesn't count.
    parts = segment.split()
    if (
        len(parts) >= 3
        and parts[1] == "-m"
        and parts[2] in _MODULE_RUNNERS
        and _looks_like_interpreter(parts[0])
    ):
        return True
    wrapper = next((w for w in _RUN_WRAPPERS if segment == w or segment.startswith(f"{w} ")), None)
    if wrapper is None:
        return False
    # A multi-word runner phrase anywhere after the wrapper (`uv run … python -m pytest`).
    if any(f" {r} " in f" {segment} " for r in _PHRASE_RUNNERS):
        return True
    # Otherwise find the wrapper's *own* command: skip its flags, and skip the value of a
    # flag that takes one. That value is the whole difficulty — in `uv run --with pytest
    # pytest -q` the first "pytest" is an install argument and the second is the command,
    # and a plain substring search cannot tell them apart. Requiring a multi-word phrase
    # used to be the guard against reading `--with pytest ruff check` as a test run; it also
    # made every `uv run --with pytest pytest -q` invisible, which is how a real repo's whole
    # test-check family went dark while the eval reported 100%.
    skip = False
    for token in segment[len(wrapper):].split():
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in _WRAPPER_VALUE_FLAGS and "=" not in token
            continue
        return token in _BARE_RUNNERS  # the first non-flag token IS the command
    return False


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


def _runner_segment(cmd: str) -> str | None:
    """The command segment that is a test/build runner, or None. Splitting on shell separators
    keeps a runner name quoted inside an echo/grep from counting."""
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
    """The argv `tycho exec` was given inside `cmd`, or None. This argv is the join between the
    two evidence streams — the only thing the transcript and the exec log both know."""
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


# Slack between the harness's event clock and our own when matching an exec run to the
# event that launched it. Generous on purpose: too tight loses real evidence, and the
# ambiguity rule below is what protects correctness, not this number.
_EXEC_CLOCK_SLACK = 5.0


def _exec_run_for(event, commands) -> "CommandRun | None":  # noqa: F821 — model.CommandRun
    """The `tycho exec` evidence for this transcript event, or None.

    Matched on the inner argv, and **an ambiguous match is no match**.

    `commands.jsonl` is repo-scoped and shared by every process in the repo, so with two
    agents working the same tree, `tycho exec -- pytest -q` appears twice with different
    outcomes. Picking the newest let agent B's passing run answer for agent A's failing one:
    VERIFIED, citing "Tycho ran it — exit 0", on a red suite. That is the fabricated green
    this program exists to prevent, and it fired precisely where the evidence is treated as
    strongest.

    So: only runs that could plausibly be *this* event's are considered — one that started
    after the event finished is a different run, whoever launched it — and if more than one
    survives, the honest answer is that we cannot tell. Losing evidence is the acceptable
    direction; inventing it is not. A turn that legitimately ran the suite twice has two
    events, and each still resolves against its own run by time.

    ponytail: argv + a time window, no process identity. `tycho exec` could stamp its pid and
    the check require it, which would be exact — worth doing if the window proves too coarse.
    """
    if not commands:
        return None
    argv = _exec_argv(event.input.get("command") or "")
    if not argv:
        return None
    # The harness stamps the event when the tool *finished*; a run that began after that
    # cannot be the one it recorded. Slack absorbs clock granularity between the two clocks.
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


def _status_is_masked(cmd: str) -> bool:
    """True when the exit status the harness recorded is *not* the runner's own.

    The shell reports one status — the last thing it ran — so three shapes overwrite it:

        pytest | tail -1      the pipeline's status is tail's (no pipefail here)
        pytest; echo done     the status is echo's; `;` discards what came before
        pytest || true        `||` swallows the failure by construction

    `&&` is safe and must NOT be flagged: `pytest && echo ok` fails when pytest fails, and it's
    the most common honest invocation there is. A wrapper is masked when its inner command is.

    Trusting a masked status is how a red suite gets reported VERIFIED — so when in doubt say
    masked and let `_outcome` fall back to the runner's own output.
    """
    parts = _SEGMENT_TOKENS.split(cmd)  # [segment, sep, segment, sep, ..., segment]
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not _is_runner(_normalize_segment(seg)):
            inner = _unwrap(seg)
            if inner is None or _runner_segment(inner) is None:
                continue  # not the runner segment, wrapped or otherwise
            if _status_is_masked(inner):
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

    Tail only: Claude Code caps `toolUseResult.stdout` at 30k chars and keeps the *head*
    (verified against 2356 real payloads) while pytest prints its
    summary last, so a truncated capture honestly reports nothing rather than matching a stray
    "5 passed" from a red run.
    """
    result = event.result or {}
    text = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr")).strip()
    return "\n".join(text.splitlines()[-_SUMMARY_TAIL_LINES:]) if text else ""


def _outcome(event, commands=()) -> bool | None:
    """Did this runner invocation fail? True = failed, False = passed, None = can't tell.

    One predicate for every caller, so no two checks can disagree about what "green" means.
    Evidence ladder, strongest first:

    1. *A status Tycho captured itself* (`tycho exec`) — Tycho was the parent and read `wait()`:
       no shell to mask it, no harness to drop or truncate it.
    2. *The transcript's exit code*, when nothing masked it. Real, but the harness had to choose
       to keep it, and three of the four often don't.
    3. *The runner's own summary line* — weakest, and the one a 30k head-truncation destroys.

    When 1 and 2 disagree, failure wins, asymmetrically: `tycho exec -- pytest && ./deploy.sh`
    can fail for a reason the capture can't see, and calling that green would be fabricated.
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
    # Say where the verdict came from: output-recovered evidence is weaker than an exit code.
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
        # Session-scoped: staleness is a fact about the tree *now*, so an uncovered source from
        # an earlier turn still counts — but word it so it doesn't imply a this-turn edit.
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
    # Judging an out-of-repo edit against this repo's diff would report it "reconciled" on
    # file_state's evidence (it exists on disk), not git's.
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
        # Point the reader at the fix — a bare "zero-config" is a dead end in the Stop output.
        return _r("scope_drift", CheckStatus.UNSUPPORTED,
                  "no [scope] set — run `tycho scope add '<glob>'` to bound edits (zero-config)")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "scope_drift",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to check scope against",
        )
    # In scope iff it matches an include glob AND no exclude glob — exclude wins.
    def _in_scope(p: str) -> bool:
        return any(fnmatchcase(p, g) for g in globs) and not any(fnmatchcase(p, g) for g in excludes)

    outside = [p for p in sorted(edited) if not _in_scope(p)]
    if outside:
        return _r("scope_drift", CheckStatus.FAIL, f"edits outside stated scope: {', '.join(outside)}")
    within = f"all edits within scope {list(globs)}"
    return _r("scope_drift", CheckStatus.PASS, f"{within} (excluding {list(excludes)})" if excludes else within)


# tool_call_provenance families: (label, claim pattern, tool-name substrings). BROAD by design —
# a claim requires *some* tool call of that family, not a content match, which no generalizable
# tool schema supports and which would risk a false FAIL. Patterns match only first-person
# past-tense claims of a *completed* action; an unclassifiable claim is simply not counted.
_PROV_WEB = re.compile(
    r"\b(?:searched (?:the web|online)|web[- ]?searched|googled|"
    r"browsed to|fetched the (?:page|url|web ?site|site))\b",
    re.IGNORECASE,
)
# A ticket anchor (KEY like ACME-123, or "Jira ticket/issue/card") near an action cue, in either
# order — prose says both "moved ACME-123 to In Progress" and "ACME-123 moved to Done". Two cue
# kinds, both too conservative to false-FAIL an honest or future turn:
#   - a *past-tense* verb (a future "I'll move ACME-123" uses base "move", never "moved");
#   - a *two-status arrow* ("Hold → In Review"), only ever written of what happened.
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

# A cue with a third-party subject or a pre-existing state is the agent *narrating* — "Dan closed
# ACME-123", "ACME-123 was already closed" — and firing on it is a false FAIL. Neutralize those
# clauses before matching; subject-dropped ("ACME-123 closed") and first-person survive. Recall
# loss is accepted over any false FAIL. The name branch is case-sensitive so a lowercase word
# ("and moved") is never mistaken for a subject; the verbs are not.
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

# An *observed* ticket state — "ACME-123 already sits at In Review → Done", "the board shows In
# Review → Done" — reports where a ticket is, not a transition the agent made; without this the
# arrow branch of _ISSUE_CUE false-FAILs it. Neutralize the stative clause *through its arrow*;
# a real self-claim keeps an action verb ("moved") and is untouched. Recall loss on a verbless
# observed arrow is accepted. The anchor stays case-sensitive so a lowercase word is never
# mistaken for a KEY.
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
    """Did the agent's prose claim a tool action that never happened?

    Deterministic, never an LLM judge: a claimed family must be backed by *some* tool call of
    that family. No prose captured, or no recognized claim, is UNSUPPORTED — never a false FAIL.
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

    Turn-scoped, not session-scoped: otherwise a read-only turn re-reports long-committed work
    as this turn's. A tool-action *claim* counts too — an MCP-only turn ("I created the ticket")
    has no edits or runners, but its claim is what tool_call_provenance exists to check.
    """
    return bool(
        session.turn_edits or _runner_events(session.turn_events) or _claimed_families(session)
    )


# --- helpers ----------------------------------------------------------------

def _r(name: str, status: CheckStatus, evidence: str) -> CheckResult:
    return CheckResult(name, status, evidence)


def _scope(session: Session) -> str:
    """What the turn-scoped checks should *call* their scope in the evidence line. No turn
    boundary means nothing narrowed the view, so the honest word is "session", not "turn"."""
    return "turn" if session.turn_start else "session"


def _runner_events(events) -> list:
    """The test/build runner invocations among ``events`` — pass the scope you mean."""
    return [
        e
        for e in events
        if e.tool in _SHELL_TOOLS and _runner_segment(e.input.get("command") or "") is not None
    ]


def _last_green_run_ts(session: Session) -> float | None:
    # Session-scoped on purpose: a run three turns back still covers a source that hasn't
    # changed since, and freshness/provenance are the checks whose job is to reason across turns.
    greens = [e.ts for e in _runner_events(session.events) if _outcome(e, session.commands) is False]
    return max(greens) if greens else None


# Prose and pictures: editing them can't invalidate a green run, and STALE sinks the whole
# verdict. Narrow on purpose — config and lockfiles stay sources, a dependency change can break
# tests.
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
    """True for a path `_relpath` left repo-relative. Out-of-repo edits stay absolute in native
    *or* POSIX flavor — the host can't judge the other — so both are tested."""
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
        # Tests WERE edited but no pre-session baseline exists (harness omitted `originalFile`,
        # git couldn't supply it). A capability gap, not an all-clear — say so distinctly.
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

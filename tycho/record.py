"""The durable per-turn record: ``<repo>/.tycho/turns.jsonl`` (strategy §9.2).

One JSON object per line, append-only, **newest last**. This is the substrate the turn
digest, ``tycho blame``/``log``, the commit trailer, and the decay ledger all read; it is
deliberately a flat file rather than a database, because greppable + stdlib + no index is
the whole reason a local verifier can promise zero dependencies and zero daemons.

Schema (``schema: 1``; the key is written **first on every line** so a migration can read
the version without parsing the rest, and so `head -c 20` on the file tells you what it is)::

    schema        int     always 1 for this shape
    id            str     16 hex chars — stable turn id, derived from session+bounds
    session       str|null  the harness's session id (null when it doesn't expose one)
    harness       str     "claude" | …
    model         str|null  model id that produced the turn — NEVER guessed
    agent_version str|null  harness/agent version — NEVER guessed
    started_at    float   epoch, turn start
    ended_at      float   epoch, turn end (when the verdict was reached)
    verdict       str     Verdict name: VERIFIED|FAILED|STALE|UNSUPPORTED|INDETERMINATE|OVERRIDDEN
    stage         str     Stage value: attempted|executed|artifact_changed|claim_supported
    checks        list    [{"name": str, "status": str, "evidence": str}]
    files         list    [{"path": str (repo-relative POSIX), "kind": "create"|"edit", "ts": float}]
    commands      list    [{"cmd": str, "runner": bool, "outcome": "passed"|"failed"|"unknown"}]
    claims        list    [str] — the agent's own prose from this turn

**Purity seam.** ``build()`` is pure: a gathered ``Session`` + check results + verdict in,
a dict out. All I/O is in ``append()``/``read()``/``touching()``/``iter_records()``.
``verify.gather()`` stays the only I/O boundary on the way in — attribution rides in on
``Session.attribution``, which gather reads through the harness adapter.

**Never raises.** This is written from the Stop hook, which must never break the agent's
turn (see ``hook.py``), so every function here follows ``state.py``'s fail-open rule: a
record we can't write is simply not written, and a file we can't read reads as empty.

**Redaction.** Transcripts contain secrets. Making a transcript durable and greppable is
exactly the moment to strip them, so command strings, check evidence and prose all go
through ``redact()`` before they hit disk — see ``_REDACTIONS``, a single table that is a
calibration knob, not a finished list. Secrets are replaced with a visible ``[REDACTED]``
so a reader knows something was removed rather than silently reading a hole.

**Retention.** The file is capped at ``max_records()`` turns (default 5000, override with
``TYCHO_TURNS_MAX``, same idiom as ``TYCHO_RELAY_MAX``). Pruning happens on append, and
only once the file has drifted ``_PRUNE_SLACK`` lines past the cap, so the common append is
one ``write()`` and never a rewrite. Individual fields are truncated too, so one pathological
turn can't write a megabyte line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from . import checks as checks_mod
from . import state
from . import verify as engine
from .model import CheckResult, Session, Stage, Verdict

SCHEMA = 1
FILE = "turns.jsonl"

# Bounds on what one record may write. Generous enough that a claim is still readable
# months later (the point of `tycho blame`), small enough that a runaway turn can't
# produce a line no reader will load.
_MAX_CLAIM_CHARS = 2000
_MAX_CLAIMS = 20
_MAX_CMD_CHARS = 500
_MAX_COMMANDS = 50
_MAX_EVIDENCE_CHARS = 500
_TRUNCATED = "…[truncated]"

_MAX_DEFAULT = 5000
# Slack before a prune: rewriting the whole file on every append once it reaches the cap
# would make every Stop O(cap). With slack, a rewrite happens once per `_PRUNE_SLACK`
# turns instead. ponytail: amortized rewrite; switch to a ring/segment file only if a
# 5000-line rewrite every 250 turns ever shows up in a profile.
_PRUNE_SLACK = 250


def max_records() -> int:
    """How many turns ``turns.jsonl`` keeps. Default 5000; ``TYCHO_TURNS_MAX`` overrides.

    Floored at 1 — "keep nothing" is not a retention policy, it's a broken config, and this
    is read inside the Stop hook, so a junk value falls back to the default rather than raising.
    """
    try:
        return max(1, int(os.environ.get("TYCHO_TURNS_MAX", _MAX_DEFAULT)))
    except (TypeError, ValueError):
        return _MAX_DEFAULT


def path_for(repo: Path) -> Path:
    """``<repo>/.tycho/turns.jsonl``, resolved the same way as the rest of Tycho's state."""
    return state.dir_for(repo) / FILE


# --- redaction ---------------------------------------------------------------
#
# One table, ordered: the specific, high-confidence shapes first (a named secret, a
# credentialed URL, a known key prefix), the generic high-entropy blob last, so a token we
# can name is redacted by the rule that names it. Each row is (pattern, replacement); the
# replacement keeps whatever identifies *what* was removed (the variable name, the header,
# the scheme) and drops only the value, because "AWS_SECRET_ACCESS_KEY=[REDACTED]" is
# evidence and a blank line is not.
#
# This is a calibration knob, not a finished list — add rows as real secrets show up. It is
# tuned to over-redact rather than under-redact: a false [REDACTED] costs a reader a little
# context, a miss puts a live credential in a durable, greppable, long-lived file.

_SECRET_NAME = (
    r"[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|"
    r"API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Za-z0-9_]*"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # FOO_TOKEN=value / "api_key": "value" — an env assignment or JSON field whose *name*
    # says secret. Quoted values are consumed whole so the closing quote isn't left dangling;
    # an unquoted value stops at a shell separator so `KEY=x && pytest` keeps its `&& pytest`
    # (eating the separator would turn a two-command line into a misleading one-command line).
    (re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)"),
     r"\1\2[REDACTED]"),
    # Authorization: Bearer xxx — the header, in a curl command or a pasted request.
    (re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s\"';]+"),
     r"\1[REDACTED]"),
    # Long-form credential flags: --password=x, --token x, --api-key x.
    (re.compile(r"(?i)(--(?:password|passwd|token|api[-_]?key|secret)(?:[= ]))\S+"), r"\1[REDACTED]"),
    # Short mysql-style `-pSECRET` (value attached, ≥6 chars, and not all-lowercase so
    # `find -printf` survives). ponytail: the space-separated `-p x` form is deliberately
    # NOT matched — `mkdir -p dir`, `docker -p 8080:80`, `cp -p src` are overwhelmingly the
    # common case, and gutting those would make the record unreadable to buy a shape almost
    # nothing writes. Add a row here if a real one shows up.
    (re.compile(r"(?<![\w-])-p(?=\S*[^a-z\s])\S{6,}"), "-p[REDACTED]"),
    # scheme://user:password@host — inline credentials in a URL (git remotes, curl, psql).
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # Vendor-prefixed keys, which are self-identifying and never anything else.
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant|or)?[-_]?[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    # Long high-entropy hex — a raw key or hash-shaped secret. Exactly 40 hex chars is
    # exempt: that is a git object id, which is the single most useful identifier a turn
    # record can carry and is not a secret. ponytail: length heuristic, not entropy; a
    # 64-char sha256 in prose is redacted, which is the safe side of that trade.
    (re.compile(r"\b(?![0-9a-fA-F]{40}\b)[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
    # Long base64-ish blob (JWT segments, encoded keys). Requires *mixed case*, which is
    # what separates an encoded blob from a long lowercase identifier — and, crucially,
    # from the 40-char lowercase git sha the rule above just went out of its way to keep.
    (re.compile(r"\b(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}"),
     "[REDACTED]"),
)


def redact(text: str) -> str:
    """Strip obvious secrets from `text`, replacing each with a visible ``[REDACTED]``.

    Applied to every free-text field before it is written (commands, check evidence, prose).
    Best-effort by construction — see ``_REDACTIONS``. Never raises.
    """
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(text: str, limit: int) -> str:
    """Redact, then bound. In that order: truncating first could cut a secret in half and
    leave the front of it on disk with nothing matching the pattern any more."""
    text = redact(str(text or ""))
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


# --- the acceptance ladder ---------------------------------------------------


def stage_of(session: Session, results: list[CheckResult] | tuple[CheckResult, ...]) -> Stage:
    """The highest rung of the acceptance ladder this turn reached (strategy §6.4). Pure.

    Descending, first match wins:

    - ``claim_supported`` — a *substantive* check PASSed. Deliberately the same bar
      ``verify.verdict_of`` uses to reach VERIFIED (``verify._WEAK_CHECKS``), reused rather
      than restated: a second definition of "substantive" would drift from the verdict's,
      which is the exact split-brain ``checks._outcome`` exists to prevent.
    - ``artifact_changed`` — a file this turn created/edited is actually on disk. "Claimed
      an edit" is not a rung; the file being there is.
    - ``executed`` — a recognized test/build runner ran this turn (``checks._runner_events``,
      which already reads through pipes, wrappers and nested shells).
    - ``attempted`` — the floor: the turn did something, but nothing above held.
    """
    if any(r.status.name == "PASS" and r.name not in engine._WEAK_CHECKS for r in results):
        return Stage.CLAIM_SUPPORTED
    if any(
        (fs := session.files.get(fe.path)) is not None and fs.exists
        for fe in session.turn_edits
    ):
        return Stage.ARTIFACT_CHANGED
    if checks_mod._runner_events(session.turn_events):
        return Stage.EXECUTED
    return Stage.ATTEMPTED


# --- build (pure) ------------------------------------------------------------


def build(
    session: Session,
    results: list[CheckResult] | tuple[CheckResult, ...],
    verdict: Verdict | str,
    harness: str,
    ended_at: float,
) -> dict:
    """One gathered turn + its results + its verdict → the record dict. **Pure**: no I/O,
    no clock, no randomness — ``ended_at`` is passed in so the same inputs always produce
    the same record (and therefore the same ``digest``).

    Redaction and truncation happen here, not in ``append``, so nothing downstream of this
    function has ever held the unredacted text.
    """
    turn_events = session.turn_events
    started_at = session.turn_start or min(
        (e.ts for e in (*turn_events, *session.turn_edits) if e.ts), default=ended_at
    )
    attribution = session.attribution
    return {
        # `schema` first, always — see the module docstring.
        "schema": SCHEMA,
        "id": _turn_id(attribution.session_id, started_at, ended_at),
        "session": attribution.session_id,
        "harness": harness,
        "model": attribution.model,
        "agent_version": attribution.agent_version,
        "started_at": started_at,
        "ended_at": ended_at,
        "verdict": str(verdict),
        "stage": str(stage_of(session, results)),
        "checks": [
            {
                "name": r.name,
                "status": r.status.name,
                "evidence": _clean(r.evidence, _MAX_EVIDENCE_CHARS),
            }
            for r in results
        ],
        # Repo-relative POSIX paths — `gather` already normalized them, so `blame` can
        # compare against what a developer types without knowing the harness's flavor.
        "files": [
            {"path": fe.path, "kind": fe.kind, "ts": fe.ts} for fe in session.turn_edits
        ],
        "commands": _commands(turn_events, session.commands),
        "claims": [
            _clean(m.text, _MAX_CLAIM_CHARS) for m in session.turn_messages[:_MAX_CLAIMS]
        ],
    }


def _turn_id(session_id: str | None, started_at: float, ended_at: float) -> str:
    """A stable id for this turn: 16 hex of sha256 over (session, bounds).

    Content-derived rather than random so rebuilding the same turn yields the same id, and
    so two processes can't mint different ids for one turn. 64 bits is far more than enough
    to keep 5000 records apart.
    """
    seed = f"{session_id or ''}|{started_at!r}|{ended_at!r}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _commands(turn_events, exec_runs=()) -> list[dict]:
    """Every shell command this turn ran: the string, whether it was a recognized
    test/build runner, and what it returned.

    The runner predicate and the outcome both come from ``checks`` — ``_runner_segment``
    and ``_outcome`` encode which wrappers hide a runner and when the shell masked its exit
    status, and a second copy of that reasoning here would eventually disagree with the
    verdict about what "passed" means. ``exec_runs`` is ``Session.commands``, so a command
    put on the record by ``tycho exec`` reports the status Tycho itself observed — the
    receipt and the verdict read the same evidence, by construction.
    """
    out = []
    for e in turn_events:
        if e.tool not in checks_mod._SHELL_TOOLS:
            continue
        cmd = e.input.get("command") or ""
        if not cmd:
            continue
        runner = checks_mod._runner_segment(cmd) is not None
        if runner:
            failed = checks_mod._outcome(e, exec_runs)
        elif checks_mod._status_is_masked(cmd):
            failed = None  # the recorded status isn't this command's — say unknown
        else:
            failed = e.is_error
        out.append({
            "cmd": _clean(cmd, _MAX_CMD_CHARS),
            "runner": runner,
            "outcome": "unknown" if failed is None else ("failed" if failed else "passed"),
        })
        if len(out) >= _MAX_COMMANDS:
            break
    return out


# --- the attestation digest --------------------------------------------------


def digest(record: dict) -> str:
    """``"sha256:<hex>"`` over a canonical serialization of `record` — the attestation.

    Canonical means sorted keys, no whitespace, UTF-8: two dicts with the same content hash
    the same regardless of key order, so a record read back off disk digests identically to
    the one that was built. (The *on-disk* line is deliberately not sorted — ``schema`` leads
    it — which is why this canonicalizes rather than hashing the raw line.)

    Reproducible: ``Tycho-Attestation: {record.digest(r)}`` yields the same trailer on any
    machine, for any process, forever, given the same record.
    """
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- I/O ---------------------------------------------------------------------


def append(repo: Path, record: dict) -> bool:
    """Append one record to ``<repo>/.tycho/turns.jsonl``; True if it landed.

    Never raises (Stop-hook rule). One ``write()`` of one line in append mode: a short line
    appended to a file opened ``"a"`` does not interleave with a concurrent appender on any
    platform Tycho supports, which is why the field bounds in ``build`` matter — they are
    what keeps the line short. ponytail: no lockfile; add one only if records ever grow past
    a page.
    """
    try:
        path = path_for(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _prune(path, max_records())
        return True
    except (OSError, TypeError, ValueError):
        return False


def _prune(path: Path, cap: int, slack: int | None = None) -> None:
    """Trim the file to its newest `cap` records, once it has drifted past cap+slack.

    `slack` is a parameter because the same amortization suits different write rates:
    turns are appended once per agent turn, `command.py`'s evidence log once per command.
    Read at call time, not bound as a default, so ``_PRUNE_SLACK`` stays patchable.
    """
    slack = _PRUNE_SLACK if slack is None else slack
    kept: deque[str] = deque(maxlen=cap)
    total = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total += 1
                kept.append(line)
        if total <= cap + slack:
            return
        # Atomic, like every other write in this package: temp sibling, then rename.
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(line if line.endswith("\n") else line + "\n" for line in kept)
        tmp.replace(path)
    except OSError:
        pass


def iter_records(repo: Path) -> Iterator[dict]:
    """Stream every record for `repo`, **oldest first**, skipping corrupt lines.

    Streaming and never-raising: a truncated final line (a crashed append), a line of
    garbage, or an unreadable file yields fewer records — never an exception. This is the
    primitive the decay ledger groups by ``model`` over.
    """
    try:
        with path_for(repo).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # corrupt line — skip it, never fail the whole read
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def read(repo: Path, limit: int | None = None) -> list[dict]:
    """The last `limit` records for `repo`, **newest first** (all of them when limit is None).

    Bounded: the file is streamed and only `limit` lines are ever held, so answering "the
    last 20" costs 20 records of memory whatever the file's size. This is what ``tycho log``
    reads.
    """
    if limit is not None and limit <= 0:
        return []
    rows: deque[dict] = deque(iter_records(repo), maxlen=limit)
    return list(reversed(rows))


def touching(repo: Path, path: str, limit: int | None = None) -> list[dict]:
    """Records whose `files` include `path`, **newest first** — the ``tycho blame`` query.

    `path` is a repo-relative POSIX path, as stored (``src/app.py``). A bare basename also
    matches the records that touched it in any directory, because that is what someone
    types mid-debug; an exact repo-relative path is the unambiguous form. Same bounded
    streaming as ``read``.
    """
    if limit is not None and limit <= 0:
        return []
    # `removeprefix`, never `lstrip("./")`: lstrip strips a character *set*, so it eats the
    # leading dot of every dotfile — `.github/workflows/ci.yml` became `github/workflows/…`
    # and matched nothing, making dotfiles silently unblameable. A false "no turn touched
    # this" is the one answer a tool built on not lying must never give.
    needle = str(path or "").replace("\\", "/").removeprefix("./")
    if not needle:
        return []
    hits: deque[dict] = deque(maxlen=limit)
    for row in iter_records(repo):
        files = row.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            stored = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(stored, str) and (stored == needle or stored.endswith("/" + needle)):
                hits.append(row)
                break
    return list(reversed(hits))

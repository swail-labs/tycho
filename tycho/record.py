"""The durable per-turn record: ``<repo>/.tycho/turns.jsonl`` (strategy §9.2).

One JSON object per line, append-only, **newest last**. The substrate the turn digest,
``tycho blame``/``log``, the commit trailer, and the decay ledger all read. A flat file
rather than a database: greppable + stdlib + no index is what buys zero dependencies.

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

**Purity seam.** ``build()`` is pure; all I/O is in
``append()``/``read()``/``touching()``/``iter_records()``, with ``verify.gather()`` the only
I/O boundary on the way in.

**Never raises.** Written from the Stop hook, which must never break the agent's turn (see
``hook.py``): a record we can't write is simply not written, a file we can't read is empty.

**Redaction.** Command strings, check evidence and prose go through ``redact()`` before
disk. Secrets become a visible ``[REDACTED]`` so a reader knows something was removed.

**Retention.** Capped at ``max_records()`` turns, pruned on append only once the file has
drifted ``_PRUNE_SLACK`` lines past the cap, so the common append is one ``write()``. Fields
are truncated too, so one pathological turn can't write a megabyte line.
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

# Bounds on what one record may write: a claim stays readable months later, a runaway turn
# can't produce a line no reader will load.
_MAX_CLAIM_CHARS = 2000
_MAX_CLAIMS = 20
_MAX_CMD_CHARS = 500
_MAX_COMMANDS = 50
_MAX_EVIDENCE_CHARS = 500
_TRUNCATED = "…[truncated]"

_MAX_DEFAULT = 5000
# Slack before a prune, so a rewrite happens once per `_PRUNE_SLACK` turns instead of making
# every Stop O(cap). ponytail: amortized rewrite; switch to a ring/segment file only if a
# 5000-line rewrite every 250 turns shows up in a profile.
_PRUNE_SLACK = 250


def _env_cap(var: str, default: int) -> int:
    """A retention cap from `var`, floored at 1 ("keep nothing" is a broken config) and
    falling back to `default` on junk — read inside the Stop hook, so it must not raise."""
    try:
        return max(1, int(os.environ.get(var, default)))
    except (TypeError, ValueError):
        return default


def max_records() -> int:
    """How many turns ``turns.jsonl`` keeps. Default 5000; ``TYCHO_TURNS_MAX`` overrides."""
    return _env_cap("TYCHO_TURNS_MAX", _MAX_DEFAULT)


def path_for(repo: Path) -> Path:
    return state.dir_for(repo) / FILE


# --- redaction ---------------------------------------------------------------
#
# Ordered: specific high-confidence shapes first, the generic high-entropy blob last, so a
# token we can name is redacted by the rule that names it. Each replacement keeps what
# identifies *what* was removed — "AWS_SECRET_ACCESS_KEY=[REDACTED]" is evidence, a blank is
# not. A calibration knob, not a finished list, tuned to over-redact: a false [REDACTED]
# costs a reader context, a miss puts a live credential in a durable, greppable file.

_SECRET_NAME = (
    r"[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|"
    r"API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Za-z0-9_]*"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # FOO_TOKEN=value / "api_key": "value". Quoted values go whole (no dangling quote);
    # unquoted ones stop at a shell separator, so `KEY=x && pytest` keeps its `&& pytest`.
    (re.compile(rf"(?i)\b({_SECRET_NAME})(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s;&|)]+)"),
     r"\1\2[REDACTED]"),
    # Authorization: Bearer xxx — the header, in a curl command or a pasted request.
    (re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s\"';]+"),
     r"\1[REDACTED]"),
    # Long-form credential flags: --password=x, --token x, --api-key x.
    (re.compile(r"(?i)(--(?:password|passwd|token|api[-_]?key|secret)(?:[= ]))\S+"), r"\1[REDACTED]"),
    # Short mysql-style `-pSECRET` (attached, ≥6 chars, not all-lowercase so `find -printf`
    # survives). ponytail: the spaced `-p x` form is deliberately NOT matched — `mkdir -p dir`,
    # `docker -p 8080:80`, `cp -p src` dominate; add a row if a real one shows up.
    (re.compile(r"(?<![\w-])-p(?=\S*[^a-z\s])\S{6,}"), "-p[REDACTED]"),
    # scheme://user:password@host — inline credentials in a URL (git remotes, curl, psql).
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # Vendor-prefixed keys, which are self-identifying and never anything else.
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant|or)?[-_]?[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    # Long high-entropy hex, except exactly 40 chars: that is a git object id, not a secret.
    # ponytail: length heuristic, not entropy, so a 64-char sha256 in prose is redacted too.
    (re.compile(r"\b(?![0-9a-fA-F]{40}\b)[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
    # Long base64-ish blob (JWT segments, encoded keys). *Mixed case* is required: it separates
    # an encoded blob from a long lowercase identifier, and from the git sha kept above.
    (re.compile(r"\b(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}"),
     "[REDACTED]"),
)


def redact(text: str) -> str:
    """Strip obvious secrets from `text`, replacing each with a visible ``[REDACTED]``.
    Applied to every free-text field before it is written. Best-effort — see ``_REDACTIONS``."""
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(text: str, limit: int) -> str:
    """Redact, then bound — truncating first could cut a secret in half and leave its front
    on disk matching no pattern."""
    text = redact(str(text or ""))
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


# --- the acceptance ladder ---------------------------------------------------


def stage_of(session: Session, results: list[CheckResult] | tuple[CheckResult, ...]) -> Stage:
    """The highest rung of the acceptance ladder this turn reached (strategy §6.4). Pure.
    Descending, first match wins:

    - ``claim_supported`` — a *substantive* check PASSed, reusing ``verify._WEAK_CHECKS`` so
      this can't drift from the bar VERIFIED uses.
    - ``artifact_changed`` — a file this turn edited is on disk. Claiming an edit is not a
      rung; the file being there is.
    - ``executed`` — a recognized test/build runner ran (``checks._runner_events``).
    - ``attempted`` — the floor.
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
    """One gathered turn + its results + its verdict → the record dict. **Pure**: ``ended_at``
    is passed in so the same inputs give the same record and the same ``digest``. Redaction
    and truncation happen here, so nothing downstream ever holds the unredacted text."""
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
        # Repo-relative POSIX paths (normalized by `gather`), so `blame` can compare against
        # what a developer types without knowing the harness's flavor.
        "files": [
            {"path": fe.path, "kind": fe.kind, "ts": fe.ts} for fe in session.turn_edits
        ],
        "commands": _commands(turn_events, session.commands),
        "claims": [
            _clean(m.text, _MAX_CLAIM_CHARS) for m in session.turn_messages[:_MAX_CLAIMS]
        ],
    }


def _turn_id(session_id: str | None, started_at: float, ended_at: float) -> str:
    """A stable id for this turn: 16 hex of sha256 over (session, bounds). Content-derived
    rather than random so rebuilding a turn yields the same id and two processes can't mint
    different ids for one turn."""
    seed = f"{session_id or ''}|{started_at!r}|{ended_at!r}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _commands(turn_events, exec_runs=()) -> list[dict]:
    """Every shell command this turn ran: the string, whether it was a recognized test/build
    runner, and what it returned. The runner predicate and the outcome both come from
    ``checks``; a second copy of that reasoning here would eventually disagree with the
    verdict about what "passed" means. ``exec_runs`` is ``Session.commands``, so a command
    recorded by ``tycho exec`` reports the status Tycho itself observed."""
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


# --- safe accessors ----------------------------------------------------------
#
# A record read back off disk may come from an older schema, a crashed append, or a hand
# edit. These coerce rather than validate: a malformed row yields less, never a traceback in
# the Stop hook.


def _rows(record: dict, key: str) -> list[dict]:
    value = record.get(key)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _claims(record: dict) -> list[str]:
    value = record.get("claims")
    if not isinstance(value, list):
        return []
    return [c.strip() for c in value if isinstance(c, str) and c.strip()]


# --- the attestation digest --------------------------------------------------


def digest(record: dict) -> str:
    """``"sha256:<hex>"`` over a canonical serialization of `record` — the attestation.

    Canonical (sorted keys, no whitespace, UTF-8) so a record read back off disk digests
    identically to the one built; the on-disk line is deliberately *not* sorted (``schema``
    leads it), which is why this hashes a re-serialization rather than the raw line."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- I/O ---------------------------------------------------------------------


def append(repo: Path, record: dict) -> bool:
    """Append one record to ``<repo>/.tycho/turns.jsonl``; True if it landed. Never raises.

    One ``write()`` of one short line in append mode does not interleave with a concurrent
    appender on any platform Tycho supports — which is what the field bounds in ``build``
    buy. ponytail: no lockfile; add one only if records grow past a page."""
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
    """Trim the file to its newest `cap` records, once it has drifted past cap+slack. `slack`
    is a parameter because turns and `command.py`'s evidence log are written at different
    rates, and read at call time so ``_PRUNE_SLACK`` stays patchable."""
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
        # Atomic: temp sibling, then rename.
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(line if line.endswith("\n") else line + "\n" for line in kept)
        tmp.replace(path)
    except OSError:
        pass


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Stream the JSON objects in a JSONL file, oldest first. A truncated final line (a
    crashed append), garbage, or an unreadable file yields fewer rows — never an exception.
    Shared with ``command.py``'s evidence log."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # corrupt line, never fail the whole read
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def iter_records(repo: Path) -> Iterator[dict]:
    """Every record this Tycho understands, oldest first.

    Filters on `schema` rather than trusting the shape, because the file outlives the
    version that wrote it: a 0.2.0 reader will meet lines a later Tycho wrote, and a key
    that changed meaning would otherwise be read as if it hadn't. Skipping is the honest
    handling — the same rule `command.py` already applies to its own log, and the same
    posture as every other reader here, which drops what it can't interpret rather than
    guessing at it.
    """
    return (r for r in iter_jsonl(path_for(repo)) if r.get("schema") == SCHEMA)


def read(repo: Path, limit: int | None = None) -> list[dict]:
    """The last `limit` records for `repo`, **newest first** (all when limit is None).
    Streamed, so only `limit` records are ever held whatever the file's size."""
    if limit is not None and limit <= 0:
        return []
    rows: deque[dict] = deque(iter_records(repo), maxlen=limit)
    return list(reversed(rows))


def touching(repo: Path, path: str, limit: int | None = None) -> list[dict]:
    """Records whose `files` include `path`, **newest first** — the ``tycho blame`` query.
    `path` is repo-relative POSIX as stored (``src/app.py``); a bare basename also matches it
    in any directory, because that is what someone types mid-debug."""
    if limit is not None and limit <= 0:
        return []
    # `removeprefix`, never `lstrip("./")`: lstrip strips a character *set*, so it ate the
    # leading dot of every dotfile and made them silently unblameable.
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

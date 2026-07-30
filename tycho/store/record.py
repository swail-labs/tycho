"""The durable per-turn record: ``<repo>/.tycho/turns.jsonl`` (strategy §9.2).

One JSON object per line, append-only, newest last — the substrate the turn digest,
``tycho blame``/``log``, the commit trailer, and the decay ledger all read. A flat file
because greppable + stdlib + no index is what buys zero dependencies.

Schema (``schema`` is written first on every line so a migration can read the version
without parsing the rest)::

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

``build()`` is pure; all I/O is in ``append()``/``read()``/``touching()``/``iter_records()``.
Nothing here raises — it runs from the Stop hook, so a record we can't write is simply not
written. Free text goes through ``redact()`` first, and every field is bounded so one
pathological turn can't write a megabyte line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from ..engine import checks as checks_mod
from . import state
from .. import engine
from ..model import CheckResult, Session, Stage, Verdict

SCHEMA = 1
FILE = "turns.jsonl"

# Bounds on what one record may write.
_MAX_CLAIM_CHARS = 2000
_MAX_CLAIMS = 20
_MAX_CMD_CHARS = 500
_MAX_COMMANDS = 50
_MAX_EVIDENCE_CHARS = 500
_TRUNCATED = "…[truncated]"

_MAX_DEFAULT = 5000
# `deque(maxlen=n)` raises OverflowError past sys.maxsize, which took the whole append down.
# A cap nobody will reach is the same as no cap.
_MAX_CEILING = 10_000_000
# Slack before a prune, so a rewrite happens once per `_PRUNE_SLACK` turns instead of making
# every Stop O(cap). ponytail: amortized rewrite; a ring file only if it shows in a profile.
_PRUNE_SLACK = 250
# Attempts at the lock before appending without it. The unlocked write keeps the promise that
# a turn is never lost, but it is the only write a concurrent prune can erase, so it is worth
# a few more tries first. Bounded: the Stop hook waits, it does not hang.
_LOCK_ATTEMPTS = 3


def _env_cap(var: str, default: int) -> int:
    """A retention cap from `var`, clamped to [1, `_MAX_CEILING`], `default` on junk."""
    try:
        return min(max(1, int(os.environ.get(var, default))), _MAX_CEILING)
    except (TypeError, ValueError):
        return default


def max_records() -> int:
    """How many turns ``turns.jsonl`` keeps. Default 5000; ``TYCHO_TURNS_MAX`` overrides."""
    return _env_cap("TYCHO_TURNS_MAX", _MAX_DEFAULT)


def path_for(repo: Path) -> Path:
    return state.dir_for(repo) / FILE


# --- redaction ---------------------------------------------------------------
#
# Ordered: named shapes first, the generic high-entropy blob last. Each replacement keeps what
# identifies *what* went — "AWS_SECRET_ACCESS_KEY=[REDACTED]" is evidence, a blank is not. A
# calibration knob tuned to over-redact: a false [REDACTED] costs context, a miss puts a live
# credential in a durable greppable file.
#
# **Every quantifier here is bounded.** Unbounded, they are quadratic on agent-controlled
# text — a `cat` of a large file hung the Stop hook for 75 seconds.

_NAME_RUN = "[A-Za-z0-9_]{0,64}"  # env var names are short; the bound is what keeps this linear
_SECRET_NAME = (
    rf"{_NAME_RUN}(?:SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|PASS|CREDENTIAL|KEY|AUTH|SALT|"
    rf"DSN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY){_NAME_RUN}"
)
_VALUE = "\"[^\"]{0,4096}\"|'[^']{0,4096}'|[^\\s;&|)]{1,4096}"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A private key block, whole — its last line is short enough to survive the base64 rule.
    (re.compile(r"(?s)(-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----).{0,65536}?"
                r"(-----END [A-Z ]{0,40}PRIVATE KEY-----)"), r"\1[REDACTED]\2"),
    # Authorization headers. Before the name=value row, which would stop at `Bearer`.
    (re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s\"';]{1,4096}"),
     r"\1[REDACTED]"),
    # FOO_TOKEN=value. Unquoted values stop at a shell separator, so `KEY=x && pytest` keeps it.
    (re.compile(rf"(?i)\b({_SECRET_NAME})([ \t]*[=:][ \t]*)({_VALUE})"), r"\1\2[REDACTED]"),
    # Long-form credential flags: --password=x, --token x, --api-key x.
    (re.compile(r"(?i)(--(?:password|passwd|token|api[-_]?key|secret)(?:[= ]))\S{1,4096}"),
     r"\1[REDACTED]"),
    # Spaced short flags, only where the command names what the value is.
    (re.compile(r"(?i)(\b(?:login|ssh-keygen)\b[^\n]{0,120}?\s-(?:p|N)\s+)"
                r"(?:\"[^\"]{0,4096}\"|'[^']{0,4096}'|\S{1,4096})"), r"\1[REDACTED]"),
    # curl/wget `-u user:password` — the user identifies the removal, the password goes.
    (re.compile(r"(?i)(\b(?:curl|wget)\b[^\n]{0,160}?\s-u\s+[^\s:]{1,64}:)\S{1,4096}"),
     r"\1[REDACTED]"),
    # Attached `-pSECRET` only. ponytail: spaced `-p x` is dominated by `mkdir -p`, `cp -p`;
    # the row above covers the commands where it really is a password.
    (re.compile(r"(?<![\w-])-p(?=\S{0,256}[^a-z\s])\S{6,4096}"), "-p[REDACTED]"),
    # scheme://user:password@host — inline credentials in a URL (git remotes, curl, psql).
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{0,32}://)[^\s/:@]{1,256}:[^\s/@]{1,256}@"),
     r"\1[REDACTED]@"),
    # Vendor-prefixed keys, which are self-identifying and never anything else.
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant|or)?[-_]?[A-Za-z0-9_-]{16,4096}"),
     "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,4096}"), "[REDACTED]"),
    (re.compile(r"\b(?:whsec_|hf_)[A-Za-z0-9_]{16,4096}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,4096}"), "[REDACTED]"),
    # Long high-entropy hex. A bare 40-char run is a git object id and is spared unless
    # something assigns it; `sha256:` is spared outright — those are our own attestations.
    # ponytail: length, not entropy, so a 64-char sha256 in prose is redacted too.
    (re.compile(r"(?<!sha256:)(?<![0-9a-fA-F])"
                r"(?:(?:(?<=[=:])|(?<=[=:] )|(?<=[=:]\")|(?<=[=:] \"))[0-9a-fA-F]{40}|(?![0-9a-fA-F]{40}(?![0-9a-fA-F]))[0-9a-fA-F]{32,})"
                r"(?![0-9a-fA-F])"), "[REDACTED]"),
    # Long base64-ish blob. Mixed case *and* a digit or +/ required, which is what separates
    # one from a long identifier, the git sha above, or a CamelCase symbol in prose.
    (re.compile(r"\b(?=[A-Za-z0-9+/]{0,512}[a-z])(?=[A-Za-z0-9+/]{0,512}[A-Z])"
                r"(?=[A-Za-z0-9+/]{0,512}[0-9+])[A-Za-z0-9+/]{40,4096}={0,2}"),
     "[REDACTED]"),
)


def redact(text: str) -> str:
    """Strip obvious secrets, each replaced with a visible ``[REDACTED]``. Best-effort."""
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


_REDACT_HEADROOM = 8  # multiples of the field limit that reach the patterns


def _clean(text: str, limit: int) -> str:
    """Bound, redact, bound — in that order.

    The first bound is a generous multiple of the limit: truncating *to* the limit could cut a
    secret in half and leave its front matching no pattern, but without any bound a 128 KB
    command string took 75 seconds in the Stop hook. A hang is not an exception, so fail-open
    never sees it.
    """
    text = str(text or "")
    headroom = limit * _REDACT_HEADROOM
    if len(text) > headroom:
        text = text[:headroom]
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


# --- the acceptance ladder ---------------------------------------------------


def stage_of(session: Session, results: list[CheckResult] | tuple[CheckResult, ...]) -> Stage:
    """The highest rung of the acceptance ladder this turn reached (strategy §6.4). Pure.

    - ``claim_supported`` — a substantive check PASSed. Shares ``verify._SUBSTANTIVE_CHECKS``
      so the ladder can't drift from the bar VERIFIED uses.
    - ``artifact_changed`` — a file this turn edited is on disk. Claiming an edit is not a rung.
    - ``executed`` — a recognized test/build runner ran.
    - ``attempted`` — the floor.
    """
    if any(r.status.name == "PASS" and r.name in engine._SUBSTANTIVE_CHECKS for r in results):
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
    """One gathered turn + results + verdict → the record dict. Pure: ``ended_at`` is passed
    in so the same inputs give the same ``digest``. Redaction happens here, so nothing
    downstream holds the unredacted text."""
    turn_events = session.turn_events
    started_at = session.turn_start or min(
        (e.ts for e in (*turn_events, *session.turn_edits) if e.ts), default=ended_at
    )
    attribution = session.attribution
    return {
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
        # Repo-relative POSIX (normalized by `gather`), so `blame` matches what a dev types.
        "files": [
            {"path": fe.path, "kind": fe.kind, "ts": fe.ts} for fe in session.turn_edits
        ],
        "commands": _commands(turn_events, session.commands),
        "claims": [
            _clean(m.text, _MAX_CLAIM_CHARS) for m in session.turn_messages[:_MAX_CLAIMS]
        ],
    }


def _turn_id(session_id: str | None, started_at: float, ended_at: float) -> str:
    """16 hex of sha256 over (session, bounds) — content-derived, so two processes can't mint
    different ids for one turn."""
    seed = f"{session_id or ''}|{started_at!r}|{ended_at!r}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _commands(turn_events, exec_runs=()) -> list[dict]:
    """Every shell command this turn ran: the string, whether it was a runner, what it
    returned. Both predicates come from ``checks`` — a second copy here would eventually
    disagree with the verdict about what "passed" means."""
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
# A row off disk may be an older schema, a crashed append, or a hand edit. These coerce
# rather than validate: less data, never a traceback in the Stop hook.


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
    """``"sha256:<hex>"`` over a canonical serialization — the attestation. Sorted keys, so a
    record read back off disk digests identically to the one built; the on-disk line leads with
    ``schema`` and is not sorted, hence the re-serialization."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # `surrogatepass`: a lone surrogate in the agent's prose makes plain `encode` raise, on a
    # row already on disk. Digesting the bytes changes no valid record's hash.
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


# --- I/O ---------------------------------------------------------------------


def append(repo: Path, record: dict) -> bool:
    """Append one record; True if it landed. Never raises.

    One ``write()`` of one short line in append mode doesn't interleave — which is what
    ``build``'s field bounds buy. The lock is for the *prune*, which rewrites the file;
    failing to take it still appends and skips the prune, because losing a turn is worse than
    losing the prune.

    That fallback is the last resort and not the first: an unlocked append is the only write a
    concurrent prune can erase, so the lock is retried before giving up on it. `_locked`
    already waits, but its deadline is a stuck holder's, and on Windows it reads progress from
    a clock too coarse to see the lock change hands.

    And an unlocked append is not only at risk from the prune. The opening sentence holds on
    POSIX, where ``O_APPEND`` makes the seek-to-end and the write one atomic step; the Windows
    CRT implements append as a seek followed by a write, so two unlocked appenders can resolve
    the same end offset and the second erases the first. Observed on CI: 4 processes x 200
    appends, 798 records on disk, Windows only — never once reproducible on macOS, where this
    same load never fails to take the lock at all (so the fallback never runs). Hence the
    read-back: on the path where the write is unprotected, confirm it survived, and re-append
    once if it did not. Bounded at one retry deliberately — a lost turn is the failure being
    fixed, but a *duplicated* turn double-counts in every tally downstream, so the blast radius
    of `_landed` being wrong stays one record either way.

    ``ensure_ascii=True`` because a lone surrogate in the agent's prose isn't encodable UTF-8,
    and the raise lost the whole turn rather than one odd character.
    """
    try:
        path = path_for(repo)
        state._private_dir(path.parent)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        state._touch_private(path)
        for _ in range(_LOCK_ATTEMPTS):
            with state._locked(path) as held:
                if not held:
                    continue  # try again for the lock before writing where a prune can lose it
                _write_line(path, line)
                _prune(path, max_records())
                return True
        # Every attempt at the lock timed out. Write without it anyway — losing a turn is
        # worse than losing the prune — but verify, because this is the write nothing guards.
        _write_line(path, line)
        if _landed(path, line):
            return True
        _write_line(path, line)
        return _landed(path, line)
    except (OSError, TypeError, ValueError):
        return False


def _write_line(path: Path, line: str) -> None:
    """One append of one terminated line. `_terminate` first, or a previous process killed
    mid-write leaves this record spliced onto its partial one."""
    _terminate(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _landed(path: Path, line: str) -> bool:
    """Is ``line`` on disk as a whole line? The check for an unguarded append.

    Only the last `_LANDED_WINDOW` lines are considered, because a match further back is a
    *different* record that happens to serialize identically, and treating that as success is
    how a genuinely lost turn would be reported as landed. The read itself is of the whole
    file — a tail seek would be the optimization, and this path only runs when the lock was
    unavailable for `_LOCK_ATTEMPTS` rounds, which never happened once on POSIX under the
    heaviest load in the suite.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-_LANDED_WINDOW:]
    except OSError:
        return False
    return line in (candidate.rstrip("\n") for candidate in tail)


# How far back `_landed` looks for its own line: enough that every concurrent appender on the
# machine could have written since this one did.
_LANDED_WINDOW = 64


def _terminate(path: Path) -> None:
    """Give the file a final newline if it lacks one. A process killed mid-append leaves an
    unterminated line, and the next append splices both records into one unparseable row."""
    try:
        if path.stat().st_size == 0:
            return
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return
        with path.open("ab") as fh:
            fh.write(b"\n")
    except OSError:
        pass


def _prune(path: Path, cap: int, slack: int | None = None) -> None:
    """Trim to the newest `cap` records once the file drifts past cap+slack. `slack` is a
    parameter because turns and `command.py`'s log are written at different rates.

    **Call this holding ``state._locked(path)``** — it is read-all-then-rename, so an append
    landing mid-flight is dropped and two concurrent prunes spliced the file to one line.

    Holding the lock is not enough on its own, because `append` writes whether or not it got
    one: losing a turn is worse than losing the prune, so a writer that times out appends
    anyway. That append lands after this read and is not in `kept`, and publishing over it
    drops a record that was already on disk — the hole this trades away by publishing only
    onto the file it read.
    """
    slack = _PRUNE_SLACK if slack is None else slack
    kept: deque[str] = deque(maxlen=cap)
    total = 0
    tmp = state._tmp_name(path)
    try:
        before = path.stat().st_size
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total += 1
                kept.append(line)
        if total <= cap + slack:
            return
        # A temp sibling named for *this* writer — a shared `<name>.tmp` publishes a splice.
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "w", encoding="utf-8") as fh:
            fh.writelines(line if line.endswith("\n") else line + "\n" for line in kept)
        _publish(tmp, path, before)
    except OSError:
        tmp.unlink(missing_ok=True)


def _publish(tmp: Path, path: Path, size_before: int) -> bool:
    """Rename `tmp` over `path`, but only if `path` is still what was read. False if it grew.

    The file only ever grows by an append, and an append this prune never saw is a record it
    would erase. Dropping the prune costs a file that stays over its cap until the next
    append; publishing anyway costs a turn. The cap is soft and a turn is not.

    ponytail: a size check, not a generation counter or an fcntl range lock. Ceiling: the
    append could still land between this stat and the rename, so it narrows the window from
    the whole read-and-rewrite to two syscalls rather than closing it.
    """
    if path.stat().st_size != size_before:
        tmp.unlink(missing_ok=True)
        return False
    state.retry_sharing(lambda: tmp.replace(path))
    return True


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Stream a JSONL file's objects, oldest first. Corrupt lines yield fewer rows, never an
    exception. Shared with ``command.py``'s evidence log."""
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
    """Every record this Tycho understands, oldest first. Filtered on `schema`, not shape: the
    file outlives the version that wrote it, and a key that changed meaning would otherwise be
    read as if it hadn't."""
    return (r for r in iter_jsonl(path_for(repo)) if r.get("schema") == SCHEMA)


def read(repo: Path, limit: int | None = None) -> list[dict]:
    """The last `limit` records, newest first. Streamed, so only `limit` are ever held."""
    if limit is not None and limit <= 0:
        return []
    rows: deque[dict] = deque(iter_records(repo), maxlen=limit)
    return list(reversed(rows))


def touching(repo: Path, path: str, limit: int | None = None) -> list[dict]:
    """Records whose `files` include `path`, newest first — the ``tycho blame`` query. A bare
    basename matches in any directory, because that is what someone types mid-debug."""
    if limit is not None and limit <= 0:
        return []
    # `removeprefix`, never `lstrip("./")` — lstrip strips a character *set*, eating the
    # leading dot of every dotfile.
    needle = str(path or "").replace("\\", "/").removeprefix("./")
    if not needle:
        return []
    # Bare basenames only: `vendor/src/app.py` is not an answer to `src/app.py`.
    bare = "/" not in needle
    hits: deque[dict] = deque(maxlen=limit)
    for row in iter_records(repo):
        files = row.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            stored = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(stored, str) and (
                stored == needle or (bare and stored.endswith("/" + needle))
            ):
                hits.append(row)
                break
    return list(reversed(hits))

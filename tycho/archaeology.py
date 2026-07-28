"""`tycho blame` and `tycho log` — agent archaeology over the turn record.

Git tells you what changed; this tells you what the agent *said* it was doing and what evidence
backed it. `blame` spends two lines per turn because it is read carefully, `log` one because it
is scanned.

**The evidence clause is the whole product.** `— no test ran` is the part only Tycho can say, so
every line ends in one, derived from the record rather than guessed: an adverse check's own
sentence, else what the turn's test runner returned, else the honest absence.

**`:LINE` is honest, not precise.** The record stores which turns touched a *file*, never which
lines, so `blame src/app.py:42` prints every turn touching the file and says attribution is
file-level. Joining lines to turns via `git blame` was rejected: it attributes to a *commit*,
and commits don't map onto turns (one commit is many turns, an uncommitted turn is no commit,
a repo need not be git). That guess would put a confident wrong name on a line.

**Exposure.** Redaction and truncation happen upstream on write in `record.py`, so this module
reads only already-redacted text. Nothing here opens a transcript or reconstructs a field from
anywhere but the record — reading around the redaction would undo it.

**Never raises.** A record file can be truncated mid-append or written by a future schema; every
field read tolerates a missing or wrong-typed value, because a developer mid-debug wants nine
good lines and a gap, not a traceback.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import record as record_mod

# ponytail: one fixed budget, not the real terminal width — `shutil.get_terminal_size` would make
# output depend on the window and on `COLUMNS` leaking into a test run. Widen if someone
# complains about a 200-column terminal.
_WIDTH = 100
_MAX_EVIDENCE = 56  # the evidence clause is the value — it gets its space before the claim
_MIN_CLAIM = 24

# Adverse statuses. Their evidence string is the truest sentence Tycho has about the turn, so it
# outranks any runner outcome in the clause below.
_ADVERSE = ("FAIL", "STALE", "INDETERMINATE")

_NO_CLAIM = "(no claim recorded)"
# Said every time an empty result is printed: the alternative reading, "the agent never touched
# this", is a stronger claim than the record can support.
_WHY_EMPTY = "Tycho records a turn each time the Stop hook fires; earlier work is not in the record."


# --- target parsing ----------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A parsed `blame` argument. `line` is carried only so the output can *acknowledge* it;
    nothing filters on it (see the module docstring)."""

    path: str
    line: int | None = None


# `PATH:LINE`, and only that: the line part must be all digits and last, so a Windows
# `C:\src\app.py` and a path that merely contains a colon are left alone.
_TARGET = re.compile(r"^(?P<path>.+?):(?P<line>\d+)$")


def parse_target(target: str) -> Target:
    """`"src/app.py:42"` → `Target("src/app.py", 42)`; anything else → the whole string."""
    match = _TARGET.match(str(target or ""))
    if not match:
        return Target(str(target or "").strip())
    return Target(match["path"].strip(), int(match["line"]))


def resolve(repo: Path, path: str, cwd: Path | None = None) -> str:
    """A path as a developer types it → the repo-relative POSIX form the record stores.

    A bare basename that doesn't exist relative to cwd is deliberately passed through untouched:
    `record.touching` matches a basename against any directory, and someone typing `app.py` from
    `src/` when the file lives in `lib/` means "find it". Everything else resolves.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    base = Path(cwd) if cwd is not None else Path.cwd()
    candidate = Path(raw) if Path(raw).is_absolute() else base / raw
    if "/" not in raw and not _exists(candidate):
        return raw  # bare basename, not here — let `touching` search every directory
    try:
        return candidate.resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        return raw  # outside the repo, or unresolvable — the literal is the best guess


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


# --- the two commands --------------------------------------------------------


def blame(repo: Path, target: str, limit: int = 10, cwd: Path | None = None,
          now: float | None = None) -> list[str]:
    """Which turns touched `target`, newest first, with the claim and the evidence.

    Two lines per turn: the metadata row, then `claim — evidence`. Bounded: `touching` streams
    the file and holds only `limit` records.
    """
    parsed = parse_target(target)
    path = resolve(repo, parsed.path, cwd)
    if not path:
        return ["tycho blame: give it a path, e.g. `tycho blame src/app.py`."]
    if limit <= 0:
        # Answering "nothing touched it" to "show me zero turns" is a lie about the record.
        return [f"tycho blame: -n wants a positive number of turns, not {limit}."]
    hits = _touching(repo, path, limit)
    if not hits:
        return _nothing_here(repo, path)
    records = [row for row, _ in hits]
    stored = [where for _, where in hits]
    now = time.time() if now is None else now

    head = [f"{path} — {_count(len(records), 'turn')}, newest first"]
    if len(set(stored)) > 1:
        head.append(f"  note: `{path}` matched {len(set(stored))} files in the record — each row "
                    "shows which.")
    if parsed.line is not None:
        # Before the results, not after: a reader who takes the first line as line-42
        # attribution has already been misled by the time a footnote arrives.
        head.append(f"  note: asked for :{parsed.line} — attribution is file-level. Tycho "
                    "records which turns touched a file, not which lines.")
    # A bare basename matches that name in any directory, so the row has to say *which* file
    # it is talking about — two files with one name are not one file with two turns.
    show_path = set(stored) != {path}
    rows = [_meta_row(r, now, where if show_path else None)
            for r, where in zip(records, stored)]
    widths = _widths(rows)
    lines = list(head)
    for record, row in zip(records, rows):
        lines.append("  " + _pad(row, widths))
        lines.append("    " + _sentence(record, indent=4))
    return lines


def log(repo: Path, limit: int = 20, verdict: str | None = None,
        since: str | None = None, now: float | None = None) -> list[str]:
    """The last `limit` turns, newest first — one line each.

    `verdict` and `since` filter **inside** the bounded stream, not after it, so
    `log --verdict FAILED -n 20` yields twenty failures rather than the failures among the last
    twenty turns — a post-filter would silently answer a different question.
    """
    limit = max(0, limit)
    if not limit:
        return []
    wanted = (verdict or "").strip().upper() or None
    cutoff = _epoch(since)
    if since and cutoff is None:
        return [f"tycho log: --since wants a date like 2026-07-01, not {since!r}."]
    rows: deque[dict] = deque(
        (r for r in record_mod.iter_records(repo) if _matches(r, wanted, cutoff)),
        maxlen=limit,
    )
    records = list(reversed(rows))
    if not records:
        if wanted or cutoff:
            return ["tycho: no recorded turn matches that filter."]
        return ["tycho: no turns recorded yet.", f"       {_WHY_EMPTY}"]
    now = time.time() if now is None else now
    cells = [
        (_ago(r.get("ended_at"), now), _id(r), _verdict(r), _stage(r), _files(r))
        for r in records
    ]
    widths = _widths(cells)
    # The claim rides as a last column so `_pad` lays it out too — appending it after a padded
    # prefix would misalign it against a short final cell.
    budget = _WIDTH - sum(widths) - 2 * len(widths) - 2  # the two quotes
    return [
        _pad(row + (f'"{_claim(record, budget)}"',), widths + [0])
        for record, row in zip(records, cells)
    ]


def _matches(record: dict, verdict: str | None, cutoff: float | None) -> bool:
    if verdict and str(record.get("verdict") or "").upper() != verdict:
        return False
    if cutoff is not None:
        ts = record.get("ended_at")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            return False
    return True


def _epoch(since: str | None) -> float | None:
    """`"2026-07-01"` → local midnight as an epoch. None when absent or unparseable.

    ponytail: ISO dates only — a "3 days ago" grammar is a parser to maintain forever.
    """
    if not since:
        return None
    try:
        day = date.fromisoformat(str(since).strip())
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day).timestamp()


def _touching(repo: Path, path: str, limit: int) -> list[tuple[dict, str]]:
    """`(record, the path it actually stored)` for turns that touched `path`, newest first.

    The match rule, and it is the whole point: a query with a directory in it matches that
    stored path **exactly**, and only a bare basename is allowed to match the name in any
    directory. `record.touching` suffix-matches every query, so `src/app.py` is answered by a
    turn that only touched `vendor/src/app.py` — a confident answer about a file nobody
    edited. Bounded: streams the record holding at most `limit` rows.
    """
    needle = str(path or "").replace("\\", "/").removeprefix("./")
    if not needle:
        return []
    bare = "/" not in needle
    hits: deque[tuple[dict, str]] = deque(maxlen=limit)
    for row in record_mod.iter_records(repo):
        for entry in record_mod._rows(row, "files"):
            stored = entry.get("path")
            if not isinstance(stored, str):
                continue
            if stored == needle or (bare and stored.endswith("/" + needle)):
                hits.append((row, stored))
                break
    return list(reversed(hits))


def _nothing_here(repo: Path, path: str) -> list[str]:
    """The empty state, split by *why* it's empty — the two cases need different next steps."""
    if not record_mod.read(repo, limit=1):
        return [f"tycho: no turns recorded in this repo yet, so nothing has touched {path}.",
                f"       {_WHY_EMPTY}"]
    return [f"tycho: no recorded turn touched {path}.",
            f"       {_WHY_EMPTY}",
            "       `tycho log` shows what is recorded here."]


# --- the row cells -----------------------------------------------------------


def _meta_row(record: dict, now: float, stored: str | None = None) -> tuple[str, ...]:
    """blame's first line. Model goes last because it is nullable (`record.py` never guesses
    attribution) and must not shift the columns before it. `stored` leads when the query was a
    bare basename: which file this row is about outranks everything else on it."""
    return (*((stored,) if stored else ()),
            _ago(record.get("ended_at"), now), _verdict(record), _stage(record),
            "turn " + _id(record), _model(record))


def _sentence(record: dict, indent: int) -> str:
    """blame's second line: `"the claim" — the evidence`, fitted to `_WIDTH`.

    Under pressure the evidence is served first: a claim's first eight words carry it, while
    evidence stops making sense cut in half. Never past leaving the claim `_MIN_CLAIM`, so a
    pathological evidence string can't swallow the line.
    """
    avail = _WIDTH - indent - 5  # 2 quotes + " — "
    claim, evidence = _claim(record, avail), _evidence(record)
    if len(claim) + len(evidence) <= avail:
        return f'"{claim}" — {evidence}'
    room = min(len(evidence), max(_MAX_EVIDENCE, avail - len(claim)), avail - _MIN_CLAIM)
    return f'"{_claim(record, avail - room)}" — {_trunc(evidence, room)}'


def _evidence(record: dict) -> str:
    """What was, or wasn't, backing this turn. First match wins, most specific first:

    1. an **adverse check** — Tycho already wrote the truest sentence available;
    2. what the turn's **test runner** returned — passed, failed, or honestly unknown;
    3. **"no test ran"** — something was checked, nothing was executed;
    4. **"never verified"** — no check could conclude *and* nothing ran. Distinct from 3:
       "nobody tested it" and "nobody could even look" are different news.
    """
    checks = record_mod._rows(record, "checks")
    for check in checks:
        if str(check.get("status")) in _ADVERSE:
            name = str(check.get("name") or "check")
            detail = str(check.get("evidence") or "").strip()
            return f"{name}: {detail}" if detail else f"{name} failed"
    runners = [c for c in record_mod._rows(record, "commands") if c.get("runner")]
    if runners:
        # Prefer the one that says the most: a failure over an unknown over a pass.
        for outcome, phrasing in (("failed", "{} failed"), ("unknown", "{} ran, exit status unknown"),
                                  ("passed", "{} passed")):
            hit = next((c for c in runners if c.get("outcome") == outcome), None)
            if hit is not None:
                return phrasing.format(_trunc(str(hit.get("cmd") or "a command"), 34))
        return f"{_trunc(str(runners[0].get('cmd') or 'a command'), 34)} ran"
    if not any(str(c.get("status")) != "UNSUPPORTED" for c in checks):
        return "never verified — no check could conclude"
    return "no test ran"


def _claim(record: dict, budget: int) -> str:
    """The agent's own prose, as one line, fitted to `budget`.

    The **last** claim, not the first: the closing message is the summary a human means by "what
    did it say it did"; earlier ones are narration mid-work.
    """
    claims = record_mod._claims(record)
    if not claims:
        return _NO_CLAIM
    first_line = next((ln.strip() for ln in claims[-1].splitlines() if ln.strip()), "")
    return _trunc(first_line, max(_MIN_CLAIM, budget)) if first_line else _NO_CLAIM


def _verdict(record: dict) -> str:
    return _paint(str(record.get("verdict") or "?"))


def _stage(record: dict) -> str:
    return str(record.get("stage") or "?")


def _id(record: dict) -> str:
    """Eight hex of the turn id — `tycho show` matches on a prefix, so this is pasteable."""
    return str(record.get("id") or "?")[:8] or "?"


def _model(record: dict) -> str:
    """`model` is nullable by design (never guessed), so absent is a real state, not a bug."""
    return str(record.get("model") or "model unknown")


def _files(record: dict) -> str:
    files = record.get("files")
    count = len(files) if isinstance(files, list) else 0
    return _count(count, "file") if count else "no files"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _ago(ts: object, now: float) -> str:
    """Relative while relative is useful, absolute once it isn't — and a foreign year gets the
    full date rather than a month that silently means a different year."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "?"
    delta = now - ts
    if delta < 60:  # also catches a future timestamp from a skewed clock
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    try:
        when = datetime.fromtimestamp(ts)
        return when.strftime("%b %d") if when.year == datetime.fromtimestamp(now).year \
            else when.strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "?"


# --- layout ------------------------------------------------------------------


def _trunc(text: str, limit: int) -> str:
    text = " ".join(str(text).split())  # collapse whitespace: one row, one line, always
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _widths(rows: list[tuple[str, ...]]) -> list[int]:
    """Column widths from the rows actually printed, not the widest possible value: padding every
    verdict to `INDETERMINATE` costs dead columns the claim would otherwise use."""
    if not rows:
        return []
    return [max(_visible(row[i]) for row in rows) for i in range(len(rows[0]))]


def _pad(row: tuple[str, ...], widths: list[int]) -> str:
    """Pad to `widths`, then drop the trailing run — colour is invisible to `len`, so
    padding is computed on the *visible* length rather than the string's."""
    return "  ".join(
        cell + " " * max(0, w - _visible(cell)) for cell, w in zip(row, widths)
    ).rstrip()


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _visible(text: str) -> int:
    return len(_ANSI.sub("", text))


def _paint(verdict: str) -> str:
    """The verdict in `status.py`'s palette, so a red badge and a red `log` line are one system.

    Never to a pipe — unlike `status.py`, this output is as likely to be `| grep` as a terminal,
    and escape codes in a grep are worse than no colour.
    """
    if not _colour():
        return verdict
    from . import status

    return status._VERDICT_COLOUR.get(verdict, "") + verdict + status._RESET


def _colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False

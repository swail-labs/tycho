"""`tycho blame` and `tycho log` — agent archaeology over the turn record (strategy §9.3).

Git tells you what changed. This tells you *what the agent said it was doing* and what
evidence backed it. The only answer available today is unreadable JSONL buried in
`~/.claude/projects`, which is to say: no answer.

`blame` before `log` on purpose (§11.3): it rides muscle memory people already have and
lands *inside* the debugging moment, where `log` is a destination you have to remember.
That ranking shows up in the output too — `blame` spends two lines per turn because it is
read carefully, `log` spends one because it is scanned.

**The evidence clause is the whole product.** `agent, turn 34 — "fixed the retry logic"`
is a worse `git log`. `— no test ran` is the part only Tycho can say, so every line here
ends in one, and it is derived from the record rather than guessed: an adverse check's own
sentence if there is one, else what the turn's test runner actually returned, else the
honest absence ("no test ran" / "never verified").

**`:LINE` is honest, not precise.** The record stores which turns touched a *file*, never
which lines — see `record.py`'s `files` entries. So `blame src/app.py:42` prints every turn
that touched `src/app.py` and says plainly that attribution is file-level. Joining lines to
turns through `git blame` was the alternative and was rejected: it would attribute a line to
a *commit*, and commits do not map onto turns (one commit is many turns, an uncommitted turn
is no commit, and a repo need not be git at all). Guessing that join would put a confident
wrong name on a line — exactly the failure every check in this codebase returns UNSUPPORTED
rather than commit.

**Exposure (§10).** These commands make durable, greppable history out of what was buried
JSONL nobody read. The mitigation lives upstream, in `record.py`: secrets are redacted and
fields truncated *on write*, so this module reads only already-redacted text. Nothing here
opens a transcript, and nothing here reconstructs a field from anywhere but the record —
reading around the redaction would undo it.

**Never raises.** A record file can be truncated mid-append, hand-edited, or written by a
future schema. `record.iter_records` already skips corrupt lines; every field read here
tolerates a missing or wrong-typed value, because a developer mid-debug wants nine good
lines and a gap, not a traceback.
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

# One fixed budget rather than the real terminal width. ponytail: `shutil.get_terminal_size`
# would make output depend on the window and on `COLUMNS` leaking into a test run; 100
# matches the repo's own line-length limit and is the width these lines were designed at.
# Widen this constant if someone actually complains about a 200-column terminal.
_WIDTH = 100
_MAX_EVIDENCE = 56  # the evidence clause is the value — it gets its space before the claim
_MIN_CLAIM = 24

# Check statuses that mean "something is wrong here". Their evidence string is the truest
# sentence Tycho has about the turn, so it outranks any runner outcome in the clause below.
_ADVERSE = ("FAIL", "STALE", "INDETERMINATE")

_NO_CLAIM = "(no claim recorded)"
# Why a file can have no history that isn't Tycho's fault. Worth saying every time: the
# alternative reading of an empty result is "the agent never touched this", which is a
# stronger claim than the record can support.
_WHY_EMPTY = "Tycho records a turn each time the Stop hook fires; earlier work is not in the record."


# --- target parsing ----------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A parsed `blame` argument: a repo-relative path, and the line the user asked about.

    `line` is carried only so the output can *acknowledge* it. Nothing filters on it — see
    the module docstring on why file-level is where the record's honesty ends.
    """

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

    Absolute, repo-relative, and relative-to-a-subdirectory all have to land on the same
    string, because `state.root_for` keys everything to the repo root while the developer
    stands wherever they were debugging (TYCHO-79's problem, one layer up).

    A **bare basename that doesn't exist relative to cwd** is deliberately passed through
    untouched: `record.touching` matches a basename against any directory, and someone who
    types `app.py` from `src/` while the file lives in `lib/` means "find it", not "look in
    src/". Anything else resolves, so `../app.py` and an absolute path both work.
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

    Two lines per turn: the metadata row (when, verdict, ladder rung, turn id, model) and
    the sentence that matters (claim — evidence). Bounded by construction: `touching`
    streams the file and holds `limit` records, so blaming a file in a 5000-turn repo costs
    ten records of memory.
    """
    parsed = parse_target(target)
    path = resolve(repo, parsed.path, cwd)
    if not path:
        return ["tycho blame: give it a path, e.g. `tycho blame src/app.py`."]
    records = _touching(repo, path, max(0, limit))
    if not records:
        return _nothing_here(repo, path)
    now = time.time() if now is None else now

    head = [f"{path} — {_count(len(records), 'turn')}, newest first"]
    if parsed.line is not None:
        # Say it before the results, not after: a reader who takes the first line as
        # line-42 attribution has already been misled by the time a footnote arrives.
        head.append(f"  note: asked for :{parsed.line} — attribution is file-level. Tycho "
                    "records which turns touched a file, not which lines.")
    rows = [_meta_row(r, now) for r in records]
    widths = _widths(rows)
    lines = list(head)
    for record, row in zip(records, rows):
        lines.append("  " + _pad(row, widths))
        lines.append("    " + _sentence(record, indent=4))
    return lines


def _touching(repo: Path, path: str, limit: int) -> list[dict]:
    """Records whose `files` include `path`, newest first.

    A thin pass-through to `record.touching` — kept as a seam only so the dotfile
    regression that once lived here has somewhere to be pinned (see
    `test_blame_finds_a_dotfile_path`). The normalization bug it used to work around is
    fixed at the source now: `record.touching` uses `removeprefix("./")`, not `lstrip`.
    """
    if limit <= 0:
        return []
    return record_mod.touching(repo, path, limit=limit)


def log(repo: Path, limit: int = 20, verdict: str | None = None,
        since: str | None = None, now: float | None = None) -> list[str]:
    """The last `limit` turns, newest first — one line each.

    `verdict` ("FAILED") and `since` ("2026-07-01") are the two filters that earn their
    place: *what went wrong* and *what happened today* are the questions people actually
    bring to a history. Both filter **inside** the bounded stream rather than after it, so
    `log --verdict FAILED -n 20` yields twenty failures rather than the failures among the
    last twenty turns — a post-filter would silently answer a different question.
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
    # The claim rides as a last column so `_pad` lays it out too — appending it after a
    # padded prefix would put it right up against a short final cell in one row and two
    # spaces further out in the next, which is exactly the misalignment columns exist to stop.
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

    ponytail: ISO dates only. `date.fromisoformat` is the whole implementation; a
    "3 days ago" grammar is a parser, and a parser is a thing to maintain forever.
    """
    if not since:
        return None
    try:
        day = date.fromisoformat(str(since).strip())
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day).timestamp()


def _nothing_here(repo: Path, path: str) -> list[str]:
    """The empty state, split by *why* it's empty — the two cases need different next steps."""
    if not record_mod.read(repo, limit=1):
        return [f"tycho: no turns recorded in this repo yet, so nothing has touched {path}.",
                f"       {_WHY_EMPTY}"]
    return [f"tycho: no recorded turn touched {path}.",
            f"       {_WHY_EMPTY}",
            "       `tycho log` shows what is recorded here."]


# --- the row cells -----------------------------------------------------------


def _meta_row(record: dict, now: float) -> tuple[str, ...]:
    """blame's first line: when, verdict, rung, turn id, model. Model last and nullable —
    `record.py` never guesses attribution, so it is routinely absent and must not shift
    the columns before it."""
    return (_ago(record.get("ended_at"), now), _verdict(record), _stage(record),
            "turn " + _id(record), _model(record))


def _sentence(record: dict, indent: int) -> str:
    """blame's second line: `"the claim" — the evidence`, fitted to `_WIDTH`.

    When both fit, nothing is cut. When they don't, the evidence is served first — a claim
    is prose whose first eight words carry it, while the evidence is a fact that stops
    making sense once it's cut in half — but never past leaving the claim `_MIN_CLAIM`, so
    a pathological evidence string can't swallow the line.
    """
    avail = _WIDTH - indent - 5  # 2 quotes + " — "
    claim, evidence = _claim(record, avail), _evidence(record)
    if len(claim) + len(evidence) <= avail:
        return f'"{claim}" — {evidence}'
    room = min(len(evidence), max(_MAX_EVIDENCE, avail - len(claim)), avail - _MIN_CLAIM)
    return f'"{_claim(record, avail - room)}" — {_trunc(evidence, room)}'


def _evidence(record: dict) -> str:
    """What was, or wasn't, backing this turn — the clause the whole command exists for.

    Descending, first match wins, most specific first:

    1. an **adverse check** — Tycho already wrote the exact sentence ("claimed edits absent
       from repo: …"), and no paraphrase of it will be truer;
    2. what the turn's **test runner** returned — passed, failed, or (a masked exit status)
       honestly unknown;
    3. **"no test ran"** — something was checked, nothing was executed;
    4. **"never verified"** — no check could conclude *and* nothing ran. The floor, and
       distinct from 3: "nobody tested it" and "nobody could even look" are different news.
    """
    checks = record.get("checks")
    checks = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    for check in checks:
        if str(check.get("status")) in _ADVERSE:
            name = str(check.get("name") or "check")
            detail = str(check.get("evidence") or "").strip()
            return f"{name}: {detail}" if detail else f"{name} failed"
    commands = record.get("commands")
    commands = [c for c in commands if isinstance(c, dict)] if isinstance(commands, list) else []
    runners = [c for c in commands if c.get("runner")]
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

    The **last** claim, not the first: `record.py` stores the turn's messages in order, and
    the closing message is the summary a human means by "what did it say it did" — the
    earlier ones are usually narration mid-work.
    """
    claims = record.get("claims")
    claims = [c for c in claims if isinstance(c, str) and c.strip()] if isinstance(claims, list) else []
    if not claims:
        return _NO_CLAIM
    first_line = next((ln.strip() for ln in claims[-1].splitlines() if ln.strip()), "")
    return _trunc(first_line, max(_MIN_CLAIM, budget)) if first_line else _NO_CLAIM


def _verdict(record: dict) -> str:
    return _paint(str(record.get("verdict") or "?"))


def _stage(record: dict) -> str:
    return str(record.get("stage") or "?")


def _id(record: dict) -> str:
    """Eight hex of the turn id — enough to be unique among 5000 records, and `tycho show`
    already matches on a prefix, so this is a handle you can paste."""
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
    """Relative while relative is useful, absolute once it isn't.

    "3d ago" beats a date inside the debugging week; past that "Jul 12" is what someone
    actually correlates against, and a foreign year gets the full date rather than a month
    that silently means a different year.
    """
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
    """Column widths from the rows actually being printed, not from the widest possible
    value. Padding every verdict to `INDETERMINATE` costs five dead columns on a screen
    where every verdict is `VERIFIED`, and the claim is what pays for them."""
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
    """The verdict in `status.py`'s palette — the same word means the same colour wherever
    Tycho draws it, so a red badge and a red `log` line are recognisably one system.

    Never to a pipe: this is the one difference from `status.py`, which writes to a harness
    that renders ANSI. Here the output is as likely to be `| grep` as a terminal, and escape
    codes in a grep are worse than no colour at all.
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

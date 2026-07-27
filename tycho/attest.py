"""The commit trailer: `Tycho-Attestation: sha256:…` (strategy §9.7/§6.6).

Covers every recorded turn that touched a file in the commit, bounded by the commit's own
timestamp. Body, and the two rendered forms::

    {"schema": 1, "turns": [<record digest>, …], "verdicts": {"VERIFIED": 3, "STALE": 1}}

    Tycho-Attestation: sha256:6f… (4 turns, 3 VERIFIED, 1 STALE)
    Tycho-Attestation: sha256:1c… (2 turns, NEVER VERIFIED: 2 UNSUPPORTED)

``git log --grep 'NEVER VERIFIED'`` is the six-months-later query. A commit with no recorded
turn gets no trailer at all. `verify` recomputes the body and answers True/False/None — None
because a pruned record must never read as a forged one.

**Never raises.** Called from a git `prepare-commit-msg` hook: a verifier that breaks
`git commit` is dead on arrival, so every path fails open.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import gitstate
from . import record as record_mod
from . import state

TRAILER = "Tycho-Attestation"
SCHEMA = 1

NEVER = "NEVER VERIFIED"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# Stable ordering so the trailer prose is deterministic across dict orderings.
_VERDICT_ORDER = ("VERIFIED", "OVERRIDDEN", "FAILED", "STALE", "INDETERMINATE", "UNSUPPORTED")

# Absorbs the epoch-second truncation git applies to its own dates.
_CLOCK_SLACK = 1.0

_TRAILER_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


# --- git (read-only, never raises) -------------------------------------------


def _git(repo: Path, *args: str) -> str | None:
    """Read-only git in `repo`; stdout on success, None on any failure — inside a commit
    hook "git is missing" and "git said no" must be the same silent answer."""
    code, out = gitstate._git(repo, *args)
    return out if code == 0 else None


def _paths_of(out: str | None) -> tuple[str, ...]:
    if not out:
        return ()
    # De-duped, order preserved: a rename shows up on two lines.
    return tuple(dict.fromkeys(
        line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()
    ))


def staged_paths(repo: Path, base: str = "HEAD") -> tuple[str, ...]:
    """Repo-relative paths the pending commit will change. ``HEAD^`` for an amend, so
    amending attests the whole rewritten commit; falls back to a bare `--cached` diff when
    the base doesn't resolve, as the root commit has no HEAD."""
    for args in ((base,), ()):
        out = _git(repo, "diff", "--cached", "--name-only", *args)
        if out is not None:
            return _paths_of(out)
    return ()


def _commit_paths(repo: Path, ref: str) -> tuple[str, ...]:
    """Paths `ref` changed against its first parent — verify's mirror of `staged_paths`."""
    return _paths_of(_git(repo, "show", "--pretty=format:", "--name-only", ref))


def _commit_time(repo: Path, ref: str) -> float | None:
    """`ref`'s committer epoch — the upper bound on which turns it could cover.

    ponytail: a rebase moves it later without re-running the hook, widening the window so an
    old trailer can read as a mismatch. Upgrade path: carry the covered turn ids in the
    trailer so verification needn't reconstruct the set."""
    out = _git(repo, "show", "-s", "--format=%ct", ref)
    try:
        return float((out or "").strip()) + _CLOCK_SLACK
    except ValueError:
        return None


# --- the attestation ---------------------------------------------------------


def attestation(repo: Path, paths, until: float | None = None) -> dict | None:
    """The attestation body for a commit touching `paths`, or None when nothing covers it.

    `until` bounds by `ended_at` so a turn recorded *after* the commit can't sneak into a
    later recomputation and break verification."""
    wanted = {str(p).replace("\\", "/") for p in paths if p}
    if not wanted:
        return None
    turns: list[str] = []
    verdicts: dict[str, int] = {}
    for row in record_mod.iter_records(repo):
        ended = row.get("ended_at")
        if until is not None and isinstance(ended, (int, float)) and ended > until:
            continue
        if not any(
            isinstance(e.get("path"), str) and e["path"] in wanted
            for e in record_mod._rows(row, "files")
        ):
            continue
        turns.append(record_mod.digest(row))
        verdict = str(row.get("verdict") or "INDETERMINATE")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    if not turns:
        return None
    return {"schema": SCHEMA, "turns": turns, "verdicts": verdicts}


def summary(body: dict) -> str:
    """Turn count and verdict tally, led by `NEVER VERIFIED` when no turn reached VERIFIED."""
    counts = body.get("verdicts") or {}
    known = [v for v in _VERDICT_ORDER if v in counts]
    tally = ", ".join(f"{counts[v]} {v}" for v in (*known, *sorted(set(counts) - set(known))))
    n = len(body.get("turns") or ())
    head = f"{n} turn{'' if n == 1 else 's'}"
    return f"{head}, {tally}" if counts.get("VERIFIED") else f"{head}, {NEVER}: {tally}"


def trailer_line(body: dict) -> str:
    """`Tycho-Attestation: sha256:… (4 turns, 3 VERIFIED, 1 STALE)` — one line, one commit."""
    return f"{TRAILER}: {record_mod.digest(body)} ({summary(body)})"


def trailer(repo: Path, source: str | None = None) -> str | None:
    """The trailer for the commit about to be made, or None when there's nothing.

    `source` is git's `prepare-commit-msg` second argument. A merge carries no work of its
    own, so no trailer; `commit` means `--amend` (or `-c`), so the base is HEAD^."""
    if source == "merge":
        return None
    body = attestation(repo, staged_paths(repo, "HEAD^" if source == "commit" else "HEAD"))
    return trailer_line(body) if body else None


# --- writing it into the commit message --------------------------------------


def _strip(text: str) -> str:
    """Drop any trailer we previously wrote, so a re-run or an amend replaces it rather
    than stacking on it."""
    return "".join(
        ln for ln in text.splitlines(keepends=True) if not ln.startswith(f"{TRAILER}:")
    )


def _append(text: str, line: str) -> str:
    """Put `line` after the prose but *before* git's comment block, which git strips and
    which would otherwise swallow the trailer."""
    lines = text.splitlines()
    cut = len(lines)
    while cut and (not lines[cut - 1].strip() or lines[cut - 1].lstrip().startswith("#")):
        cut -= 1
    body, tail = lines[:cut], lines[cut:]
    # Normalize the gap to exactly one blank line, re-added below; without it a second pass
    # keeps adding blanks and the run isn't idempotent.
    while tail and not tail[0].strip():
        tail.pop(0)
    if tail:
        tail = ["", *tail]
    if not body:
        # Empty message (editor commit, subject not typed yet): two blank lines, so whatever
        # the user types on line 1 still gets its separating blank line.
        block = ["", "", line]
    elif len(body) > 1 and _TRAILER_SHAPE.match(body[-1]):
        # Already a trailer block (`Signed-off-by: …`) — join it. `len(body) > 1` is
        # load-bearing: a conventional-commit subject (`fix: tweak`) is *exactly*
        # trailer-shaped, and gluing the attestation onto it wrecks every `git log --oneline`.
        block = [*body, line]
    else:
        block = [*body, "", line]
    return "\n".join([*block, *tail]) + "\n"


def write_message(repo: Path, msg_path: str | Path, source: str | None = None) -> bool:
    """Stamp the trailer into git's commit-message file. True if the file changed.

    **Never raises and never fails a commit**: every failure returns False, message
    untouched. An existing trailer is only removed when there's a new one to replace it, so
    a pruned record downgrades to "stale trailer", never "dropped attestation"."""
    path = Path(msg_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    line = trailer(repo, source)
    if line is None:
        return False
    updated = _append(_strip(text), line)
    if updated == text:
        return False
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


# --- verification ------------------------------------------------------------


def claimed_digest(message: str) -> str | None:
    """The `sha256:…` a commit message claims, or None. Last trailer wins."""
    found = None
    for line in message.splitlines():
        if line.startswith(f"{TRAILER}:") and (hit := _DIGEST_RE.search(line)):
            found = hit.group(0)
    return found


def verify(repo: Path, ref: str = "HEAD") -> tuple[bool | None, str]:
    """Check a commit's trailer against the record. (True | False | None, one line).

    None is a first-class answer: reporting "no attestation" or "the record no longer
    reaches back this far" as a mismatch would be a false accusation."""
    short = (_git(repo, "rev-parse", "--short", ref) or ref).strip() or ref
    message = _git(repo, "log", "-1", "--format=%B", ref)
    if message is None:
        return None, f"{short}: no such commit (or not a git repo)"
    claimed = claimed_digest(message)
    if claimed is None:
        return None, f"{short}: no Tycho attestation — not agent-written, or Tycho wasn't installed"
    body = attestation(repo, _commit_paths(repo, ref), _commit_time(repo, ref))
    if body is None:
        return None, (
            f"{short}: claims {claimed[:14]}… but the turn record no longer covers this commit "
            f"(pruned, or .tycho/ is gone) — cannot confirm"
        )
    actual = record_mod.digest(body)
    if actual == claimed:
        return True, f"{short}: attestation VERIFIED against the record — {summary(body)}"
    return False, (
        f"{short}: attestation does NOT match the record — commit claims {claimed[:14]}…, "
        f"the record gives {actual[:14]}… ({summary(body)})"
    )


# --- entrypoint: the git hook calls `<python> -m tycho.attest <msgfile> <source>` ---------


def main(argv: list[str] | None = None) -> int:
    """`[--write] <msgfile> [<source>]` — git's `prepare-commit-msg` argv, verbatim.

    **Always returns 0**: a verifier that can fail a commit is one people uninstall."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--write":
        args = args[1:]
    if args:
        try:
            write_message(
                state.root_for(Path.cwd()), args[0], args[1] if len(args) > 1 else None
            )
        except Exception:  # noqa: BLE001 — nothing may escape into `git commit`
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main())

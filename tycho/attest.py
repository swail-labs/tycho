"""The commit trailer: `Tycho-Attestation: sha256:…` (strategy §9.7/§6.6).

Cheap, rides git, permanent, `git log`-visible, and portable without a PR gate — the
strategy explicitly *demotes* the blocking GitHub Action (§6, "What not to build"), so this
is the whole distribution story for the attestation. Solo-useful six months later: it tells
you which commits were agent-written, and which of those were **never verified by anything**.

**What a commit's attestation covers.** Not one turn: a commit routinely spans fifteen. It
covers *every recorded turn that touched a file in this commit*, bounded above by the
commit's own timestamp. Concretely the body is::

    {"schema": 1, "turns": [<record digest>, …], "verdicts": {"VERIFIED": 3, "STALE": 1}}

and the trailer carries ``record.digest(body)`` — the same canonical-JSON sha256 the turn
records use, so nothing new had to be invented and the two can't drift. The turn digests are
themselves content-derived, which makes the attestation transitive: it pins the exact claims,
files, commands and verdicts of every turn behind the commit, not a hand-wave over them.

**The never-verified case is representable, not omitted.** That case *is* the stated solo
value, so the trailer says it out loud rather than going quiet::

    Tycho-Attestation: sha256:6f… (4 turns, 3 VERIFIED, 1 STALE)
    Tycho-Attestation: sha256:1c… (2 turns, NEVER VERIFIED: 2 UNSUPPORTED)

``git log --grep 'NEVER VERIFIED'`` is the six-months-later query. A commit with *no* recorded
turn behind it gets **no trailer at all** — silence is the honest answer for "a human wrote
this", "Tycho wasn't installed yet", or a merge commit that carries no work of its own.

**Verification** (`verify`) recomputes the body from `.tycho/turns.jsonl` and the commit's own
diff, and compares. Three answers, never two: True (matches), False (does not), None (cannot
tell — no trailer, or the record no longer reaches back that far). "Cannot tell" is a distinct
answer on purpose; a pruned record must never read as a forged one.

**Never raises.** This is called from a git `prepare-commit-msg` hook, so it obeys the same
rule as `hook.py`: a verifier that breaks `git commit` is dead on arrival. Every path here
fails open — no trailer, never an exception, never a non-zero exit on the write path.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from . import record as record_mod
from . import state

TRAILER = "Tycho-Attestation"
SCHEMA = 1

# The grep target for "agent-written and nothing ever confirmed it" (strategy §6.6).
NEVER = "NEVER VERIFIED"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# Stable ordering for the human summary so the trailer text is deterministic across
# platforms and dict orderings — the digest already is, and the prose must match it.
_VERDICT_ORDER = ("VERIFIED", "OVERRIDDEN", "FAILED", "STALE", "INDETERMINATE", "UNSUPPORTED")

# A commit's timestamp is written *after* the hook that composed its trailer ran, so every
# record that existed at write time satisfies `ended_at <= commit time`. One second of slack
# absorbs the epoch-second truncation git applies to its own dates.
_CLOCK_SLACK = 1.0

# `Key: value` — RFC-822-ish, the shape git itself calls a trailer. Used only to decide
# whether a blank line is needed before ours.
_TRAILER_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


# --- git (read-only, never raises) -------------------------------------------


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only git command in `repo`; stdout on success, None on any failure.

    Deliberately not `gitstate._git`: this module runs inside a commit hook, where "git is
    missing" and "git said no" must be the same, silent answer. Same read-only posture as
    `gitstate` — nothing here ever writes to the repo.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _paths_of(out: str | None) -> tuple[str, ...]:
    if not out:
        return ()
    # De-duped, order preserved: a rename shows up on two lines, and `--name-only` over a
    # multi-parent range can repeat a path.
    return tuple(dict.fromkeys(
        line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()
    ))


def staged_paths(repo: Path, base: str = "HEAD") -> tuple[str, ...]:
    """Repo-relative paths the pending commit will change.

    `base` is HEAD for a normal commit and ``HEAD^`` for an amend, so amending attests the
    whole rewritten commit rather than only the delta being folded in. Falls back to a bare
    `--cached` diff when the base doesn't resolve — the root commit has no HEAD.
    """
    for args in ((base,), ()):
        out = _git(repo, "diff", "--cached", "--name-only", *args)
        if out is not None:
            return _paths_of(out)
    return ()


def _commit_paths(repo: Path, ref: str) -> tuple[str, ...]:
    """Paths `ref` changed against its first parent — the verify-side mirror of `staged_paths`."""
    return _paths_of(_git(repo, "show", "--pretty=format:", "--name-only", ref))


def _commit_time(repo: Path, ref: str) -> float | None:
    """`ref`'s committer epoch — the upper bound on which turns it could possibly cover.

    Committer date rather than author date because it tracks when the commit *object* was
    made, which is what `--amend` moves and what the trailer is rewritten alongside.
    ponytail: a rebase moves it later without re-running the hook, so a turn recorded between
    the original commit and the rebase can widen the window and make an old trailer read as a
    mismatch. Upgrade path if that ever bites: carry the covered turn ids in the trailer so
    verification stops having to reconstruct the set.
    """
    out = _git(repo, "show", "-s", "--format=%ct", ref)
    try:
        return float((out or "").strip()) + _CLOCK_SLACK
    except ValueError:
        return None


# --- the attestation ---------------------------------------------------------


def attestation(repo: Path, paths, until: float | None = None) -> dict | None:
    """The attestation body for a commit touching `paths`, or None when nothing covers it.

    Pure over the record file: same records + same paths + same bound ⇒ same dict ⇒ same
    digest, on any machine, forever. `until` bounds by `ended_at` so a turn recorded *after*
    the commit can never sneak into a later recomputation and break verification.
    """
    wanted = {str(p).replace("\\", "/") for p in paths if p}
    if not wanted:
        return None
    turns: list[str] = []
    verdicts: dict[str, int] = {}
    for row in record_mod.iter_records(repo):
        ended = row.get("ended_at")
        if until is not None and isinstance(ended, (int, float)) and ended > until:
            continue
        files = row.get("files")
        if not isinstance(files, list):
            continue
        if not any(
            isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"] in wanted
            for e in files
        ):
            continue
        turns.append(record_mod.digest(row))
        verdict = str(row.get("verdict") or "INDETERMINATE")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    if not turns:
        return None
    return {"schema": SCHEMA, "turns": turns, "verdicts": verdicts}


def summary(body: dict) -> str:
    """The human half of the trailer: turn count and verdict tally, led by `NEVER VERIFIED`
    when not one turn behind this commit ever reached VERIFIED."""
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
    """The trailer for the commit that is about to be made, or None when there's nothing.

    `source` is git's own `prepare-commit-msg` second argument. A **merge** carries no work
    of its own — its content was attested on the commits being merged — so it gets no
    trailer. `commit` means `--amend` (or `-c`), so the base is HEAD^ and the attestation
    covers the whole rewritten commit.
    """
    if source == "merge":
        return None
    body = attestation(repo, staged_paths(repo, "HEAD^" if source == "commit" else "HEAD"))
    return trailer_line(body) if body else None


# --- writing it into the commit message --------------------------------------


def _strip(text: str) -> str:
    """Drop any trailer we previously wrote. Idempotency for a re-run, correctness for an
    amend: the message comes back with the old line in it, and the new one must replace it
    rather than stack on it."""
    return "".join(
        ln for ln in text.splitlines(keepends=True) if not ln.startswith(f"{TRAILER}:")
    )


def _append(text: str, line: str) -> str:
    """Put `line` at the end of the message body — after the prose, *before* git's comment
    block (`# Please enter the commit message…`, the `# ------ >8 ------` scissors), which
    git strips and which would otherwise swallow the trailer.
    """
    lines = text.splitlines()
    cut = len(lines)
    while cut and (not lines[cut - 1].strip() or lines[cut - 1].lstrip().startswith("#")):
        cut -= 1
    body, tail = lines[:cut], lines[cut:]
    # Normalize the gap between body and comment block to exactly one blank line, re-added
    # below. Without this the run is not idempotent: the blank we insert before our trailer
    # is *also* in `tail`, so a second pass adds another, and another.
    while tail and not tail[0].strip():
        tail.pop(0)
    if tail:
        tail = ["", *tail]
    if not body:
        # An empty message (an editor commit, subject not typed yet). Two blank lines, so
        # whatever the user types on line 1 still gets its separating blank line.
        block = ["", "", line]
    elif len(body) > 1 and _TRAILER_SHAPE.match(body[-1]):
        # Already a trailer block (`Signed-off-by: …`) — join it rather than splitting it.
        # `len(body) > 1` is load-bearing: a conventional-commit subject (`fix: tweak`,
        # `feat: add x`) is *exactly* trailer-shaped, and treating line 1 as a trailer glues
        # the attestation onto the subject — which is then the whole commit summary in every
        # `git log --oneline` forever. The subject always gets its blank line.
        block = [*body, line]
    else:
        block = [*body, "", line]
    return "\n".join([*block, *tail]) + "\n"


def write_message(repo: Path, msg_path: str | Path, source: str | None = None) -> bool:
    """Stamp the trailer into git's commit-message file. True if the file changed.

    The `prepare-commit-msg` entrypoint. **Never raises and never fails a commit**: an
    unreadable message file, an unwritable one, a repo with no records, a repo where Tycho
    was never installed — each returns False and leaves the message exactly as it was. An
    existing trailer is only removed when there is a new one to put in its place, so a pruned
    record downgrades to "stale trailer", never to "silently dropped attestation".
    """
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
    """The `sha256:…` a commit message claims, or None. Last trailer wins, so a message that
    somehow carries two is answered by the newest."""
    found = None
    for line in message.splitlines():
        if line.startswith(f"{TRAILER}:") and (hit := _DIGEST_RE.search(line)):
            found = hit.group(0)
    return found


def verify(repo: Path, ref: str = "HEAD") -> tuple[bool | None, str]:
    """Check a commit's trailer against the record. (True | False | None, one line).

    None is a first-class answer, not a soft failure: "this commit has no attestation" and
    "the record no longer reaches back this far" are both *unknown*, and reporting either as
    a mismatch would turn ordinary history and ordinary pruning into a false accusation.
    """
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


# --- entrypoint --------------------------------------------------------------
#
# `attest.py` carries its own `__main__` so the git hook has something to call that works
# today: `<python> -m tycho.attest <msgfile> <source>`. `tycho attest --write/--verify` can
# route straight here once cli.py grows the flags (see the report) — the argv shapes below
# accept both spellings, so the installed hook command never has to change.


def main(argv: list[str] | None = None) -> int:
    """`--write <msgfile> [<source>]` | `--verify [<ref>]` | bare (print the trailer).

    The write path **always returns 0**. It runs inside `git commit`, and a verifier that can
    make a commit fail is one people uninstall.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    repo = state.root_for(Path.cwd())
    if args and args[0] == "--verify":
        ok, line = verify(repo, args[1] if len(args) > 1 else "HEAD")
        print(line)
        return 1 if ok is False else 0
    if args and args[0] == "--write":
        args = args[1:]
    if args:  # <msgfile> [<source> [<sha>]] — git's own prepare-commit-msg argv, verbatim
        try:
            write_message(repo, args[0], args[1] if len(args) > 1 else None)
        except Exception:  # noqa: BLE001 — nothing may escape into `git commit`
            pass
        return 0
    line = trailer(repo)
    if line is None:
        print("tycho: nothing staged that a recorded turn touched — nothing to attest.")
        return 0
    print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main())

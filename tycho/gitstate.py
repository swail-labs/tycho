"""Read-only git reader via subprocess. Never writes to the repo."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run a git command in `repo`; return (returncode, stdout). Never raises on
    non-zero — callers decide what a failure means."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def is_repo(repo: Path) -> bool:
    code, _ = _git(repo, "rev-parse", "--git-dir")
    return code == 0


def head_sha(repo: Path) -> str | None:
    code, out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else None


def commit_exists(repo: Path, ref: str) -> bool:
    code, _ = _git(repo, "cat-file", "-e", f"{ref}^{{commit}}")
    return code == 0


def diff_names(repo: Path, since: str) -> tuple[str, ...]:
    """Paths changed between `since` and the working tree (staged + unstaged)."""
    code, out = _git(repo, "diff", "--name-only", since)
    if code != 0:
        return ()
    return tuple(line for line in out.splitlines() if line)


def blob_at(repo: Path, ref: str, path: str) -> str | None:
    """File contents at a ref (e.g. `HEAD`), or None if absent there."""
    code, out = _git(repo, "show", f"{ref}:{path}")
    return out if code == 0 else None


def untracked(repo: Path) -> tuple[str, ...]:
    """Paths git doesn't know about yet, honouring .gitignore.

    `git diff` cannot see these, and a brand-new file an agent just wrote is exactly the
    thing a review must not silently omit — so `tycho review` asks for them separately
    (TYCHO-125). Non-ignored only: reviewing `node_modules` is how a review gets closed.
    """
    code, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if code != 0:
        return ()
    return tuple(line for line in out.splitlines() if line)


# --- diff hunks --------------------------------------------------------------
#
# File-level granularity is a much weaker statement than it looks: "this file wasn't
# covered" is unactionable on a 900-line file, where "lines 88-114 weren't" is a place to
# put your eyes. So `review` needs actual hunks, which means parsing unified diff — git has
# no plumbing that hands you line ranges directly (TYCHO-125).
#
# Parsed off `git diff`'s own text with the counts in the `@@` header as the authority for
# where a hunk body ends, rather than "until a line that doesn't start with +/-/space" — a
# blank context line and a diff of a diff both break the latter. Anything this parser does
# not recognize degrades to a whole-file entry marked `unparsed`, never to silence and never
# to a guessed range: an invented line number in a review tool is worse than no line number.

MAX_HUNKS = 2000  # a bound, not a policy — see `parse_hunks`

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """One contiguous run of changed lines, addressed on the side that still exists.

    `start`/`end` are 1-based and inclusive, and are the *changed* lines only — the
    surrounding context git printed is trimmed off, because context is what you read, not
    what you review. Both are 0 for a hunk that has no line range at all (`binary`,
    `renamed` with no edit, `unparsed`), which `ref` renders as a bare path.

    `status` is one of: modified | added | deleted | renamed | binary | unparsed | untracked.
    """

    path: str
    start: int
    end: int
    added: int
    removed: int
    status: str = "modified"
    old_path: str | None = None

    @property
    def ref(self) -> str:
        """`file:line-range` — clickable in most terminals, greppable in all of them."""
        if not self.start:
            return self.path
        return f"{self.path}:{self.start}" if self.start == self.end else (
            f"{self.path}:{self.start}-{self.end}"
        )

    @property
    def size(self) -> int:
        return self.added + self.removed


def diff_hunks(repo: Path, since: str = "HEAD", limit: int = MAX_HUNKS) -> tuple[Hunk, ...] | None:
    """Changed hunks between `since` and the working tree, or **None** when git couldn't say.

    None is not "no changes" — it is "unknown ref, no commits yet, not a repo, git blew up".
    Callers must keep those apart; reporting a bad ref as a clean diff would be the tool
    quietly claiming everything is fine.
    """
    code, out = _git(
        repo,
        "-c", "core.quotePath=false",  # keep non-ASCII paths readable rather than \xNN-escaped
        "diff", "--no-color", "--no-ext-diff", "--find-renames", "--unified=3", since,
    )
    if code != 0:
        return None
    return parse_hunks(out, limit=limit)


def parse_hunks(text: str, limit: int = MAX_HUNKS) -> tuple[Hunk, ...]:
    """Unified diff text → hunks. Pure, total, and never raises on malformed input.

    `limit` caps the result: a 40k-hunk refactor is not something anyone reviews hunk by
    hunk, and an unbounded parse turns a review into a hang. Callers compare against the
    limit to say the list was cut.
    """
    hunks: list[Hunk] = []
    path: str | None = None
    old_path: str | None = None
    status = "modified"
    produced = False

    def flush() -> None:
        # A pure rename (or a mode change) has no `@@` at all, yet the file did change —
        # emit it with no range rather than dropping it.
        nonlocal produced
        if path and not produced and status in ("renamed", "deleted", "added"):
            hunks.append(Hunk(path, 0, 0, 0, 0, status, old_path))
        produced = False

    lines = text.splitlines()
    i = 0
    while i < len(lines) and len(hunks) < limit:
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            flush()
            path, old_path = _header_paths(line)
            status = "modified"
            continue
        if line.startswith("new file mode"):
            status = "added"
            continue
        if line.startswith("deleted file mode"):
            status = "deleted"
            continue
        if line.startswith("rename from "):
            old_path, status = line[len("rename from "):], "renamed"
            continue
        if line.startswith("rename to "):
            path, status = line[len("rename to "):], "renamed"
            continue
        if line.startswith("--- "):
            old_path = _strip_prefix(line[4:]) or old_path
            continue
        if line.startswith("+++ "):
            path = _strip_prefix(line[4:]) or path
            continue
        if line.startswith(("Binary files ", "GIT binary patch")):
            if path:
                hunks.append(Hunk(path, 0, 0, 0, 0, "binary", old_path))
                produced = True
            continue
        m = _HUNK_RE.match(line)
        if m is None:
            if line.startswith("@@") and path and not produced:
                # A combined diff (`@@@`, from a merge) or a shape we don't know. Say so.
                hunks.append(Hunk(path, 0, 0, 0, 0, "unparsed", old_path))
                produced = True
            continue
        if not path:
            continue
        hunk, i = _read_body(lines, i, m, path, old_path, status)
        if hunk is not None:
            hunks.append(hunk)
            produced = True
    flush()
    return tuple(hunks)


def _read_body(lines, i, m, path, old_path, status):
    """Consume one hunk body, returning (hunk|None, next index).

    Walks both cursors at once so a deletion is addressable on the new side too — where a
    removed line *was* is still a place to look, and a hunk that only deletes is otherwise
    unaddressable. The header's counts decide when the body ends.
    """
    old_start, old_left = int(m.group(1)), int(m.group(2) or 1)
    new_start, new_left = int(m.group(3)), int(m.group(4) or 1)
    new_len = new_left
    o_cur, n_cur = old_start, new_start
    o_first = o_last = n_first = n_last = 0
    added = removed = 0
    while i < len(lines) and (old_left > 0 or new_left > 0):
        body = lines[i]
        if body.startswith("\\"):  # "\ No newline at end of file" — belongs to neither side
            i += 1
            continue
        tag = body[:1]
        if tag == "+" and new_left > 0:
            added, new_left = added + 1, new_left - 1
            n_first, n_last, n_cur = n_first or n_cur, n_cur, n_cur + 1
        elif tag == "-" and old_left > 0:
            removed, old_left = removed + 1, old_left - 1
            o_first, o_last, o_cur = o_first or o_cur, o_cur, o_cur + 1
            n_first, n_last = n_first or n_cur, max(n_last, n_cur)
        elif tag in (" ", "") and old_left > 0 and new_left > 0:
            old_left, new_left = old_left - 1, new_left - 1
            o_cur, n_cur = o_cur + 1, n_cur + 1
        else:
            break  # truncated or malformed body — stop here rather than mis-count
        i += 1
    if not added and not removed:
        return None, i
    if new_len == 0 or status == "deleted":
        start, end = o_first or old_start, o_last or old_start
        status = "deleted" if status == "deleted" else status
    else:
        start, end = n_first or new_start, n_last or new_start
    start = max(1, start)
    return Hunk(path, start, max(start, end), added, removed, status, old_path), i


def _header_paths(line: str) -> tuple[str | None, str | None]:
    """Best-effort (new, old) from `diff --git a/x b/y`; the `---`/`+++` lines correct it."""
    rest = line[len("diff --git "):]
    cut = rest.rfind(" b/")
    if cut <= 0:
        return None, None
    return _strip_prefix(rest[cut + 1:]), _strip_prefix(rest[:cut])


def _strip_prefix(raw: str) -> str | None:
    """`a/foo.py` → `foo.py`; `/dev/null` → None. Quotes stripped, escapes left alone."""
    raw = raw.rstrip("\t ").strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 1:
        raw = raw[1:-1]
    if not raw or raw == "/dev/null":
        return None
    return raw[2:] if raw[:2] in ("a/", "b/", "i/", "w/", "c/", "o/") else raw

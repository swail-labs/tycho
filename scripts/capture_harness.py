#!/usr/bin/env python3
"""Capture a real harness session into the eval corpus. Dev tool — not shipped.

Every reader bug found so far came from a shape nobody would have invented: Codex spelling one
shell run two ways, a `{cmd:"…"}` regex dropping 49 exec calls of a single session, OpenCode's
milliseconds read as seconds. Hand-authored fixtures cannot catch those, because the author
writes down the shape they already believe in. Only captured transcripts can.

So this exists to make capturing cheap enough that it actually happens:

    python scripts/capture_harness.py claude --repo /path/to/scratch-repo
    python -m pytest tests/test_harness_conformance.py --update-goldens
    git diff tests/fixtures/harness/          # <- read this, it is the re-verification

That diff is what `VERIFIED_AGAINST` currently asks a human to perform from memory.

**Capture from a scratch repo.** Point `--repo` at a throwaway project the harness was run in
for this purpose. The scrubber below is belt-and-braces, not the primary defense — the primary
defense is that nothing private was in the session to begin with. This repo is public, and a
later edit does not remove anything from git history.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tycho.read import harness as harness_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "harness"

# The home path as harnesses encode it into a directory name: every separator turned into a
# dash (`/Users/me/projects/x` -> `-Users-me-projects-x`). Claude and Cursor both key their
# transcript directories this way, so the literal path substitution below misses it entirely —
# which is how the first capture attempt still carried a username after "scrubbing".
_ENCODE = str.maketrans({c: "-" for c in "\\/:. "})

# Redactions applied to every captured byte. Ordered widest-context first: the full home path,
# then its encoded spelling, then the bare username, so the specific forms are replaced with
# something readable before the blunt fallback catches whatever is left.
_SCRUB = (
    (re.compile(re.escape(str(Path.home()))), "/Users/me"),
    (re.compile(re.escape(str(Path.home()).translate(_ENCODE))), "-Users-me"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "me@example.com"),
    # Blunt on purpose, and last: a username can appear in a git author line, a venv path or a
    # log message, and this repo is public.
    (re.compile(re.escape(Path.home().name)), "me"),
    (re.compile(r"\b(sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}"), r"\1REDACTED"),
    (re.compile(r'"(api[_-]?key|token|secret|password)"\s*:\s*"[^"]*"', re.I), r'"\1": "REDACTED"'),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIAREDACTED"),
)

# What must not survive scrubbing, checked *after* substitution.
_FORBIDDEN = (
    (str(Path.home()), "the capturing user's home directory"),
    (Path.home().name, "the capturing user's username"),
)


def scrub(text: str) -> str:
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return text


def assert_clean(text: str) -> None:
    """Refuse to write a capture that still carries anything identifying.

    An assertion rather than a warning: a warning during a capture is read once and the file
    gets committed anyway, and the cost of being wrong here is permanent and public.
    """
    for needle, what in _FORBIDDEN:
        if needle and needle in text:
            raise SystemExit(f"refusing to write: capture still contains {what} ({needle!r})")


def probe_version(name: str) -> str:
    """The harness's own reported version, or the pin if it ships none to ask for."""
    pinned = harness_mod.VERIFIED_AGAINST.get(name) or {}
    probe = pinned.get("probe")
    if not probe:
        return pinned.get("version", "unknown")
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"could not probe {name} version via {probe}: {exc}")
    reported = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    # `claude --version` answers "2.1.220 (Claude Code)"; keep the number, drop the branding.
    match = re.search(r"\d+[\w.\-]*", reported)
    return match.group(0) if match else reported or "unknown"


def locate(name: str, repo: Path, explicit: Path | None) -> Path:
    path = explicit or harness_mod.BY_NAME[name].discover(repo)
    if path is None:
        raise SystemExit(
            f"no {name} transcript found for {repo}. Run a session there first, or pass "
            f"--transcript. (For OpenCode the session is rebuilt from opencode.db.)"
        )
    return path


def capture(name: str, repo: Path, explicit: Path | None, dest_name: str | None) -> None:
    if name not in harness_mod.BY_NAME:
        raise SystemExit(f"unknown harness {name!r}; known: {', '.join(harness_mod.BY_NAME)}")

    source = locate(name, repo, explicit)
    body = scrub(source.read_text(encoding="utf-8", errors="replace"))
    assert_clean(body)

    out_dir = CORPUS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (dest_name or f"transcript{source.suffix}")
    target.write_text(body, encoding="utf-8")

    version = probe_version(name)
    events = harness_mod.BY_NAME[name].parse(target)
    (out_dir / "capture.json").write_text(
        json.dumps(
            {
                "version_captured": version,
                "captured_by": "scripts/capture_harness.py",
                "source_events": len(events),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"captured {name} {version} -> {target.relative_to(REPO_ROOT)}")
    print(f"  {len(events)} events parsed")
    if not events:
        print("  WARNING: zero events — the reader saw nothing in this session, which is "
              "either an empty turn or exactly the drift this capture exists to catch")
    print("\nnext: pytest tests/test_harness_conformance.py --update-goldens && "
          "git diff tests/fixtures/harness/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("harness", help=f"one of: {', '.join(harness_mod.BY_NAME)}")
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(),
        help="scratch repo the session was run in (default: cwd)",
    )
    parser.add_argument(
        "--transcript", type=Path, default=None,
        help="explicit transcript path, instead of discovering the newest",
    )
    parser.add_argument(
        "--as", dest="dest_name", default=None,
        help="filename in the corpus dir (e.g. lying_turn.jsonl); default transcript.<ext>",
    )
    args = parser.parse_args()
    capture(args.harness, args.repo.resolve(), args.transcript, args.dest_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

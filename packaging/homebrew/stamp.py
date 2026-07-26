#!/usr/bin/env python3
"""Stamp the Homebrew formula template with a release's version + sha256 sums (TYCHO-105).

The template (`tycho.rb`, beside this file) carries `REPLACE_ME_*` markers; the `homebrew-publish`
release job runs this against the `SHA256SUMS` asset the `release` job (TYCHO-104) attached, then
pushes the result to `swail-labs/homebrew-tap` as `Formula/tycho.rb`.

Same manifest the npm wrapper verifies against (packaging/npm/bin/tycho.js) — one artifact set,
one checksum source of truth. Stdlib only, like the rest of Tycho.

    python packaging/homebrew/stamp.py 0.1.0 SHA256SUMS -o Formula/tycho.rb
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).with_name("tycho.rb")

# Formula marker -> the release asset whose sum fills it. Homebrew is macOS/Linux only, so the
# Windows tarball (which the installer + npm paths do use) has no marker here.
ASSETS = {
    "REPLACE_ME_SHA256_MACOS_ARM64": "tycho-macos-arm64.tar.gz",
    "REPLACE_ME_SHA256_MACOS_X86_64": "tycho-macos-x86_64.tar.gz",
    "REPLACE_ME_SHA256_LINUX_X86_64": "tycho-linux-x86_64.tar.gz",
}

_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})[ \t]+[* ]?(.+?)\s*$")


def expected_hash(sums_text: str, filename: str) -> str | None:
    """The sha256 for `filename` from a `sha256sum`-format manifest, lowercase hex, or None.

    Handles the binary marker (`*name`), CRLF, and stray whitespace. Matches the whole name only —
    a prefix must never false-match a different entry (mirrors expectedHash in the npm wrapper).
    """
    for line in sums_text.splitlines():
        m = _SUM_LINE.match(line)
        if m and m.group(2) == filename:
            return m.group(1).lower()
    return None


def stamp(template: str, version: str, sums_text: str) -> str:
    """Fill every marker in `template`, or raise ValueError. Never returns a partial formula:
    a missing sum here would ship a formula that fails to install for that arch.
    """
    if not version or version.startswith("v"):
        raise ValueError(f"version must be bare semver, not a tag: {version!r}")

    out = template.replace("REPLACE_ME_VERSION", version)
    for marker, asset in ASSETS.items():
        digest = expected_hash(sums_text, asset)
        if digest is None:
            raise ValueError(f"SHA256SUMS has no entry for {asset}")
        out = out.replace(marker, digest)

    left = sorted(set(re.findall(r"REPLACE_ME[A-Z0-9_]*", out)))
    if left:
        raise ValueError(f"unstamped markers left in the formula: {', '.join(left)}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="release version, bare (0.1.0), not the tag (v0.1.0)")
    ap.add_argument("sums", type=Path, help="path to the release's SHA256SUMS")
    ap.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    args = ap.parse_args(argv)

    try:
        formula = stamp(
            TEMPLATE.read_text(encoding="utf-8"),
            args.version,
            args.sums.read_text(encoding="utf-8"),
        )
    except (OSError, ValueError) as e:
        print(f"stamp: {e}", file=sys.stderr)
        return 1

    if args.out:
        args.out.write_text(formula, encoding="utf-8")
    else:
        sys.stdout.write(formula)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

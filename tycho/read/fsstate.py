"""Filesystem reader: existence, content hash, mtime. Read-only."""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 65536


def exists(path: Path | str) -> bool:
    return Path(path).is_file()


def sha256(path: Path | str) -> str | None:
    """Hex digest of the file, or None if it isn't a readable file."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def mtime(path: Path | str) -> float | None:
    """Modification time (epoch seconds), or None if the file is absent."""
    p = Path(path)
    return p.stat().st_mtime if p.is_file() else None

"""Read OpenCode sessions straight from its SQLite store (``opencode.db``).

OpenCode persists every session in ``~/.local/share/opencode/opencode.db``
(tables ``session`` / ``message`` / ``part``). Don't shell out to ``opencode export``
instead: it truncates at 128 KB whenever stdout is a pipe (Bun non-blocking-write quirk),
so every non-trivial session comes back as invalid JSON.

We reconstruct the exact ``{info, messages:[{parts:[…]}]}`` JSON that
``events.parse_opencode`` already consumes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path


def db_path() -> Path:
    """Location of OpenCode's SQLite store.

    ``TYCHO_OPENCODE_HOME`` (the dir holding ``opencode.db``) wins, then ``XDG_DATA_HOME``,
    then ``~/.local/share``. Spelled out here, not via ``harness.home``, because OpenCode's
    root is an XDG data dir rather than a ``~/.<name>`` dotdir.
    """
    override = os.environ.get("TYCHO_OPENCODE_HOME")
    if override:
        return Path(override).expanduser() / "opencode.db"
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "opencode" / "opencode.db"


def _connect(db: Path) -> sqlite3.Connection:
    """Open the DB read-only — never write to the agent's live store."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def latest_session(cwd: Path, db: Path | None = None) -> str | None:
    """Id of the most-recently-updated session rooted at ``cwd`` (or None)."""
    db = db or db_path()
    if not db.is_file():
        return None
    try:
        with _connect(db) as conn:
            row = conn.execute(
                "SELECT id FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 1",
                (str(cwd),),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row["id"] if row else None


def _json_object(raw: object) -> dict | None:
    """Parse a DB ``data`` column into a dict, or None if it isn't one (external data)."""
    try:
        value = json.loads(raw)  # type: ignore[arg-type]
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def session_json(session_id: str, cwd: Path, db: Path | None = None) -> dict | None:
    """Reconstruct one session's export-shaped JSON from the DB (tool parts only).

    Each ``part.data`` row is already the object ``opencode export`` nests under
    ``messages[].parts[]``, carrying ``state.time`` and ``state.metadata.exit``. Non-tool
    parts are dropped; ``parse_opencode`` ignores them anyway.

    One message in, one message out — parts grouped by ``part.message_id``. Don't flatten
    into one synthesized assistant message: that drops user messages, and their timestamps
    are what ``events.turn_start_opencode`` anchors the turn boundary on (they carry no
    tool parts, so they land here with ``parts: []``).

    ``info`` keeps only ``role`` and ``time``: a user message's ``data`` also carries
    ``summary.diffs`` (full patches) that nothing downstream reads.
    """
    db = db or db_path()
    if not db.is_file():
        return None
    try:
        with _connect(db) as conn:
            info = conn.execute(
                "SELECT id, directory, version FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            messages = conn.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created ASC",
                (session_id,),
            ).fetchall()
            rows = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created ASC",
                (session_id,),
            ).fetchall()
    except sqlite3.Error:
        return None

    parts_by_message: dict[str, list[dict]] = {}
    for row in rows:
        part = _json_object(row["data"])
        if part is not None and part.get("type") == "tool":
            parts_by_message.setdefault(row["message_id"], []).append(part)

    return {
        "info": {
            "id": session_id,
            "directory": info["directory"] if info else str(cwd),
            "version": info["version"] if info else "",
        },
        "messages": [
            {
                "info": {k: v for k, v in data.items() if k in ("role", "time")},
                "parts": parts_by_message.get(row["id"], []),
            }
            for row in messages
            if (data := _json_object(row["data"])) is not None
        ],
    }


def materialize(session_id: str, cwd: Path, db: Path | None = None) -> Path | None:
    """Write a session's reconstructed JSON to a temp file; return its path.

    The caller hands the path to the standard engine and unlinks it after.
    """
    data = session_json(session_id, cwd, db)
    if data is None:
        return None
    fd, name = tempfile.mkstemp(suffix=".json", prefix="tycho-opencode-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return Path(name)

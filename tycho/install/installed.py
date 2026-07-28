"""What is currently wired in this repo, and on this machine.

Separate from `spelling` because answering it means reading a config file, and `configfile`
already depends on `spelling` to know which entries are ours.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .configfile import ConfigRefused, _current_text, _load
from .spelling import (
    GLOBAL,
    _PLUGIN_MARKER,
    _is_tycho_hook,
    _is_tycho_owned,
    config_path,
    settings_path,
)


def global_installed() -> bool:
    """Is Tycho wired into the *user-level* Claude config right now? The config is the
    truth, not a marker we wrote: hand-removing the entry is uninstalling. Never raises —
    unparseable reads as "not installed", since we'd refuse to write it."""
    try:
        data = _load(settings_path(Path.cwd(), GLOBAL))
    except ConfigRefused:
        return False
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return any(
        isinstance(entry, dict) and _is_tycho_owned(entry.get("command"))
        for group in (hooks.get("Stop") or [])
        if isinstance(group, dict)
        for entry in (group.get("hooks") or [])
    )


def installed_command(repo: Path, name: str) -> str | None:
    """The tycho hook command `name` would actually run, read back from its own config —
    what the harness will really execute, not what we meant to install. Raises
    ConfigRefused if the config can't be read."""
    path = config_path(repo, name)
    if name == "opencode":
        text = _current_text(path)
        if text is None or _PLUGIN_MARKER not in text:
            return None
        # The plugin holds its argv as a JSON array: `const command = ["...", "hook"]`.
        match = re.search(r"^const command = (\[.*\])$", text, re.MULTILINE)
        if not match:
            return None
        try:
            return shlex.join(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError):
            return None
    data = _load(path)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    if name == "cursor":
        entries = hooks.get("stop") or []
    else:
        entries = [h for g in (hooks.get("Stop") or []) if isinstance(g, dict) for h in (g.get("hooks") or [])]
    for entry in entries:
        if isinstance(entry, dict) and _is_tycho_hook(entry.get("command")):
            return entry["command"]
    return None

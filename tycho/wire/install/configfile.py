"""Reading and writing a harness's config file, defensively.

Every mutation runs against a real developer's real settings, so the rules are the same
everywhere: refuse what we can't parse rather than "recovering" it to `{}`, back up first,
write atomically, and skip the write when the file is already correct.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .spelling import _is_tycho_owned


# "We declined to touch this file" — the CLI matches on this constant for its exit code.
REFUSED = ": refused — "


_BACKUP_SUFFIX = ".tycho.bak"


_TMP_SUFFIX = ".tycho-tmp"


class ConfigRefused(Exception):
    """This file must not be written — malformed, unreadable, or read-only. The message
    always carries the fix: whoever sees it is mid-install."""


def _current_text(path: Path) -> str | None:
    """What's on disk, or None if there's no file. Unreadable is refused, not ignored."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigRefused(f"cannot read {path} ({exc.strerror}) — fix the permissions and re-run") from exc


def _load(path: Path) -> dict:
    """Existing config as a dict. Missing or empty is {}; unparseable is refused, because
    treating malformed JSON as `{}` and writing that back is how a verifier eats a
    developer's whole config."""
    text = _current_text(path)
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigRefused(
            f"{path} is not valid JSON (line {exc.lineno}: {exc.msg}) — fix or move it, then re-run"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigRefused(f"{path} holds {type(data).__name__}, not a JSON object — leaving it alone")
    return data


def _hooks_of(data: dict, path: Path) -> dict:
    """The config's `hooks` object, or {} when there is none. Anything else is a shape we
    can't merge into: replacing it wholesale would delete hooks the user wrote."""
    hooks = data.get("hooks")
    if hooks is None or hooks == {}:
        return {}
    if not isinstance(hooks, dict):
        raise ConfigRefused(
            f"{path}: `hooks` holds {type(hooks).__name__}, not an object — Tycho can't merge "
            f"into that. Fix or move it, then re-run"
        )
    return dict(hooks)


def _groups_of(hooks: dict, key: str, path: Path) -> list:
    """The list under `hooks.<key>`, or [] when absent. A string here would be merged
    character by character and a bare group object flattened to its own key names, so
    refuse anything that isn't a list of groups."""
    groups = hooks.get(key)
    if groups is None:
        return []
    nested = [g.get("hooks") for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
    if not isinstance(groups, list) or any(e is not None and not isinstance(e, list) for e in nested):
        raise ConfigRefused(
            f"{path}: `hooks.{key}` isn't the list of hook groups Tycho understands — fix or "
            f"move it, then re-run"
        )
    return groups


def _write_text(path: Path, text: str, backup: bool = True) -> bool:
    """Write `text` to `path` atomically; return False if it was already correct. Resolves
    symlinks first, so a settings file symlinked into a dotfiles repo stays a symlink. Temp
    file plus rename, so a run killed mid-write leaves the original whole. ``backup=False``
    only for files wholly ours; user data always keeps a `.bak`."""
    target = Path(os.path.realpath(path))
    current = _current_text(target)
    if current == text:
        return False  # already correct: no write, no backup, no mtime churn
    if current is not None and not os.access(target, os.W_OK):
        # A rename lands regardless of the file's mode, so a deliberate `chmod -w` would
        # otherwise be silently undone.
        raise ConfigRefused(f"{target} is read-only — chmod +w (or move it), then re-run")
    tmp = target.with_name(target.name + _TMP_SUFFIX)
    try:
        if current is not None and backup:
            # .bak undoes *this* change, not a frozen original, so overwriting is correct.
            shutil.copy2(target, target.with_name(target.name + _BACKUP_SUFFIX))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        if current is not None:
            shutil.copymode(target, tmp)  # the user's permissions, not our umask's
        tmp.replace(target)
    except OSError as exc:
        # A filesystem refusal is a refusal, not a traceback. The original is still whole.
        tmp.unlink(missing_ok=True)
        raise ConfigRefused(f"cannot write {target} ({exc.strerror}) — fix that, then re-run") from exc
    return True


def _drop_backup(path: Path) -> None:
    """A clean uninstall leaves nothing behind — including the `.bak` of the state we just
    reverted. Best-effort: a backup we can't delete is litter, not a failed uninstall."""
    try:
        path.with_name(path.name + _BACKUP_SUFFIX).unlink(missing_ok=True)
    except OSError:
        pass


def _write(path: Path, data: dict) -> bool:
    return _write_text(path, json.dumps(data, indent=2) + "\n")


def _status(label: str, what: str, path: Path, command: str, existed: bool, changed: bool) -> str:
    if not changed:
        return f"{label}: {what} already current → {path}"
    verb = "updated" if existed else "installed"
    return f"{label}: {verb} {what} → {path}  ({command})"


def _strip_claude_tycho(groups: list) -> tuple[list, bool]:
    """Drop any existing tycho hook entries (and now-empty groups); flag if any removed."""
    out, existed = [], False
    for group in groups:
        entries = (group.get("hooks") or []) if isinstance(group, dict) else []
        kept = [h for h in entries if not (isinstance(h, dict) and _is_tycho_owned(h.get("command")))]
        existed = existed or len(kept) != len(entries)
        if not isinstance(group, dict) or not entries:
            # Nothing of ours in here, and possibly no `hooks` key at all — a group we don't
            # recognize goes back byte-for-byte. Rebuilding it added an empty `hooks: []` to
            # entries the user wrote, which is a rewrite of something Tycho does not own.
            out.append(group)
        elif kept:
            out.append({**group, "hooks": kept})
    return out, existed


# --- uninstall ---------------------------------------------------------------
# Never delete a harness's config file — it may hold settings we didn't write. Remove our
# entries, then drop a container only when it's empty *and* we would have created it. The
# OpenCode plugin is the exception: that whole file is ours.


def _prune(data: dict, hooks: dict, key: str, kept: list) -> dict:
    """Put `kept` back under `key`, dropping containers we emptied."""
    if kept:
        hooks[key] = kept
    else:
        hooks.pop(key, None)  # init created this list; don't leave an empty one behind
    merged = {**data, "hooks": hooks}
    if not hooks:
        merged.pop("hooks", None)
    return merged

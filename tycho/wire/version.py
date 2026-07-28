"""Is a newer Tycho published? A best-effort, offline-safe update check.

**Never runs in the Stop hook and never blocks.** One stdlib HTTPS GET to the package index
with a short timeout; anything that goes wrong returns None and the caller says nothing.
Cached machine-wide, so the network is hit at most once a day.

Surfaced only on manual paths (`tycho doctor`, `tycho init`, SessionStart bootup) — the
status bar reads the cache only, never the network.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from .. import __version__
from ..store import state

# `tycho` is taken on PyPI, so we publish and check as `tycho-cli`; the import package and
# CLI command stay `tycho`. None makes the check inert (queries nothing, never notifies).
_DIST_NAME: str | None = "tycho-cli"
_CHECK_TTL = 86400  # re-hit the index at most once a day
_TIMEOUT = 2.0  # seconds
_OPT_OUT_ENV = "TYCHO_NO_UPDATE_CHECK"


def _index_url() -> str | None:
    return f"https://pypi.org/pypi/{_DIST_NAME}/json" if _DIST_NAME else None


def _env_opted_out() -> bool:
    return os.environ.get(_OPT_OUT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _fetch() -> str | None:
    """The latest version from PyPI's JSON API, or None on any failure (or no name yet)."""
    url = _index_url()
    if not url:
        return None  # distribution name unset — see _DIST_NAME
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": f"tycho/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 — https literal
            info = json.loads(resp.read().decode("utf-8")).get("info") or {}
        version = info.get("version")
        return version if isinstance(version, str) and version else None
    except Exception:
        return None  # offline, slow, blocked, unparseable — say nothing


def refresh(force: bool = False) -> str | None:
    """Newest published version, hitting the network only if the cache is stale (≤ once/day)
    and the user hasn't opted out. Never raises.

    `force=True` bypasses the daily cache — for explicit paths only (`tycho update`,
    `doctor`); passive paths leave it False so they add no network round-trip."""
    if _env_opted_out():
        return None
    cache = state.read_update_cache()
    if not force and cache.get("latest") and (time.time() - cache.get("checked_at", 0)) < _CHECK_TTL:
        return cache["latest"]
    got = _fetch()
    if got:
        state.write_update_cache(latest=got, checked_at=time.time())
        return got
    return cache.get("latest")


def cached_latest() -> str | None:
    """The newest version already known from the cache — no network."""
    try:
        got = state.read_update_cache().get("latest")
        return got if isinstance(got, str) and got else None
    except Exception:
        return None


def _tuple(v: str) -> tuple[int, ...]:
    """Comparable release tuple: "0.1.0" -> (0, 1, 0, 1); "0.2.0rc1" -> (0, 2, 0, 0, 1).

    PyPI normalizes `0.2.0-rc.1` to `0.2.0rc1`, so a pre-release arrives as one part with
    the marker glued to the number. Keeping only the digits collapsed that to `01` -> 1,
    making `0.2.0rc1` compare *newer* than `0.2.0` — so anyone running an RC would never be
    told the real release had landed, which is the one moment an upgrade notice exists for.

    The trailing `1`/`0` is the pre-release rank: a final release sorts above any RC of the
    same version, and RCs sort among themselves by number.
    """
    parts, pre = v.strip().split("."), None
    out: list[int] = []
    for part in parts:
        digits = ""
        for i, c in enumerate(part):
            if c.isdigit():
                digits += c
                continue
            pre = "".join(ch for ch in part[i:] if ch.isdigit()) or "0"
            break
        out.append(int(digits or 0))
        if pre is not None:
            break
    return (*out, 0, int(pre)) if pre is not None else (*out, 1)


def is_newer(candidate: str, than: str) -> bool:
    return _tuple(candidate) > _tuple(than)


def notice(refresh_first: bool = False, force: bool = False) -> str | None:
    """One line if a newer version exists and the user hasn't opted out or dismissed it, else
    None. `refresh_first=True` permits the network check; the default reads only the cache,
    which is what the status bar must use. Never raises."""
    if _env_opted_out():
        return None
    try:
        newest = refresh(force=force) if refresh_first else state.read_update_cache().get("latest")
        if not isinstance(newest, str) or not is_newer(newest, __version__):
            return None
        if state.read_update_cache().get("dismissed_version") == newest:
            return None  # they waved this exact version off
        return (
            f"A newer Tycho is available: {newest} (you have {__version__}). "
            f"Run `tycho update`; dismiss with `tycho update --skip` or set {_OPT_OUT_ENV}=1."
        )
    except Exception:
        return None

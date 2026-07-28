"""Load and manage `.tycho.toml`. Zero-config default just works (no scope check, no disables).

The stdlib has no TOML writer, so the mutating helpers re-render the whole file from a
canonical template. Cost: a user's own comments and unknown tables are NOT preserved. Only
the schema keys (`scope.include`/`exclude`, `checks.disable`, `relay.enabled`,
`override.enabled`, `rewrite.enabled`) round-trip.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import state

CONFIG_NAME = ".tycho.toml"


@dataclass(frozen=True)
class Config:
    scope_include: tuple[str, ...] = ()
    disabled_checks: tuple[str, ...] = ()
    scope_exclude: tuple[str, ...] = ()
    relay_enabled: bool = False
    override_enabled: bool = False
    rewrite_enabled: bool = False


def path(repo: Path) -> Path:
    """The config for `repo` — resolved like its sibling `.tycho/`, so a command run from a
    subdirectory reads the repo's real scope instead of "no scope set"."""
    return state.root_for(repo) / CONFIG_NAME


def load(repo: Path) -> Config:
    """Read the repo's `.tycho.toml`; missing file -> defaults (never a false positive)."""
    p = path(repo)
    if not p.is_file():
        return Config()
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    scope = data.get("scope", {})
    disabled = data.get("checks", {}).get("disable", [])
    relay = data.get("relay", {}).get("enabled", False)
    override = data.get("override", {}).get("enabled", False)
    rewrite = data.get("rewrite", {}).get("enabled", False)
    return Config(
        scope_include=tuple(scope.get("include", [])),
        disabled_checks=tuple(disabled),
        scope_exclude=tuple(scope.get("exclude", [])),
        relay_enabled=bool(relay),
        override_enabled=bool(override),
        rewrite_enabled=bool(rewrite),
    )


# --- writing (stdlib-only, canonical render) --------------------------------

def _toml_str(s: str) -> str:
    """One glob as a TOML basic string. Glob metacharacters need no escaping; only a
    backslash or double-quote does."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_array(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(i) for i in items) + "]"


def render(
    scope_include: tuple[str, ...] = (),
    disabled: tuple[str, ...] = (),
    scope_exclude: tuple[str, ...] = (),
    relay_enabled: bool = False,
    override_enabled: bool = False,
    rewrite_enabled: bool = False,
) -> str:
    """The canonical `.tycho.toml`, with the current values filled in."""
    return (
        "# .tycho.toml — Tycho configuration. Managed by `tycho scope` / `tycho relay`; also safe\n"
        "# to hand-edit. Tycho never assumes this file exists: with an empty `include` below,\n"
        "# `scope_drift` stays UNSUPPORTED and every other check runs as it does with no file.\n"
        "\n"
        "[scope]\n"
        "# Directories/files the agent is allowed to edit, as globs with POSIX separators\n"
        '# (e.g. "src/**", "tests/**"). An edit outside these FAILs scope_drift. This is an\n'
        "# explicit, deterministic bound — never inferred from the prompt.\n"
        "# Manage with: tycho scope add|remove|set <glob>...  (or /tycho-scope-* in Claude Code)\n"
        f"include = {_toml_array(scope_include)}\n"
        "# Paths to carve back OUT of `include`, same glob syntax. An edit matching any of these\n"
        '# FAILs scope_drift even if it also matches `include` — exclude wins (e.g. include "**",\n'
        '# exclude "LICENSE" allows the whole tree but that one file). Empty = pure allowlist.\n'
        "# Manage with: tycho scope add|remove|set --exclude <glob>...\n"
        f"exclude = {_toml_array(scope_exclude)}\n"
        "\n"
        "[checks]\n"
        "# Check names to disable entirely (rarely needed), e.g. \"command_execution\".\n"
        f"disable = {_toml_array(disabled)}\n"
        "\n"
        "[relay]\n"
        "# Feed a non-VERIFIED verdict back to the agent so it keeps working until VERIFIED\n"
        "# (Claude Code only, bounded by TYCHO_RELAY_MAX). Off by default — verdicts stay\n"
        "# human-only. Toggle: tycho relay --on|--off  (or /tycho-relay-on / -off in Claude Code).\n"
        f"enabled = {'true' if relay_enabled else 'false'}\n"
        "\n"
        "[override]\n"
        "# Let the agent record a per-check verdict override (`tycho override <check> \"<reason>\"`)\n"
        "# when the relay is on and it can justify that a check does not apply. Produces the\n"
        "# distinct OVERRIDDEN verdict (agent-authorized, NOT proven) and is logged to\n"
        "# .tycho/overrides.json. Off by default. Toggle: tycho override --on|--off.\n"
        f"enabled = {'true' if override_enabled else 'false'}\n"
        "\n"
        "[rewrite]\n"
        "# Route a piped test runner through `tycho exec` so its real exit status survives:\n"
        '# `pytest | tail -20` hands the harness tail\'s status and pytest\'s is gone before any\n'
        "# check could read it. Claude Code only (PreToolUse). Off by default because your\n"
        "# `Bash(...)` permission rules are matched against the *rewritten* command, so a\n"
        "# `Bash(pytest:*)` allow rule would stop covering it and a silent run would start\n"
        "# asking. Not needed under `--permission-mode bypassPermissions`, where there are no\n"
        "# rules to void and the rewrite always applies. Toggle: tycho rewrite --on|--off.\n"
        f"enabled = {'true' if rewrite_enabled else 'false'}\n"
    )


def _write(repo: Path, text: str) -> None:
    """Atomic write so an interrupted run can't truncate the file."""
    p = path(repo)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def ensure(repo: Path) -> bool:
    """Create `.tycho.toml` if absent, never clobbering. True if it created the file."""
    if path(repo).exists():
        return False
    _write(repo, render())
    return True


def _rewrite_with(repo: Path, **changed) -> None:
    """Re-render `.tycho.toml` with the current settings, overridden by `changed`. One place
    to add a key, so a new flag can't be dropped by a setter that predates it."""
    cur = load(repo)
    fields = {
        "scope_include": cur.scope_include,
        "disabled": cur.disabled_checks,
        "scope_exclude": cur.scope_exclude,
        "relay_enabled": cur.relay_enabled,
        "override_enabled": cur.override_enabled,
        "rewrite_enabled": cur.rewrite_enabled,
    }
    _write(repo, render(**{**fields, **changed}))


def _save(repo: Path, include: tuple[str, ...], exclude: tuple[str, ...]) -> None:
    """Rewrite with these include/exclude lists, preserving the other settings."""
    _rewrite_with(repo, scope_include=tuple(include), scope_exclude=tuple(exclude))


def set_relay(repo: Path, enabled: bool) -> None:
    """Write the verdict-relay flag, creating the file if absent so it stays hand-editable."""
    _rewrite_with(repo, relay_enabled=enabled)


def set_override(repo: Path, enabled: bool) -> None:
    """Write the override-capability flag, creating the file if absent."""
    _rewrite_with(repo, override_enabled=enabled)


def set_rewrite(repo: Path, enabled: bool) -> None:
    """Write the runner-rewrite flag, creating the file if absent."""
    _rewrite_with(repo, rewrite_enabled=enabled)


def set_scope(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Replace the include list wholesale (dedup, keep order)."""
    new = tuple(dict.fromkeys(globs))
    _save(repo, new, load(repo).scope_exclude)
    return new


def add_scope(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Append include globs not already present."""
    cur = load(repo)
    new = tuple(dict.fromkeys((*cur.scope_include, *globs)))
    _save(repo, new, cur.scope_exclude)
    return new


def remove_scope(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Drop every listed include glob (exact match)."""
    cur = load(repo)
    drop = set(globs)
    new = tuple(g for g in cur.scope_include if g not in drop)
    _save(repo, new, cur.scope_exclude)
    return new


def set_exclude(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Replace the exclude list wholesale (dedup, keep order)."""
    new = tuple(dict.fromkeys(globs))
    _save(repo, load(repo).scope_include, new)
    return new


def add_exclude(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Append exclude globs not already present."""
    cur = load(repo)
    new = tuple(dict.fromkeys((*cur.scope_exclude, *globs)))
    _save(repo, cur.scope_include, new)
    return new


def remove_exclude(repo: Path, globs: list[str]) -> tuple[str, ...]:
    """Drop every listed exclude glob (exact match)."""
    cur = load(repo)
    drop = set(globs)
    new = tuple(g for g in cur.scope_exclude if g not in drop)
    _save(repo, cur.scope_include, new)
    return new

"""The repo-local toggles in `.tycho.toml`: scope bounds, the verdict relay, the verdict
override. Each command edits and reports; bare invocation only reports.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import ExitCode

def _scope(cwd: Path, action: str, paths: list[str], exclude_globs: list[str] | None = None) -> int:
    """`tycho scope list|set|add|remove [--exclude GLOB...]` — read or edit the scope_drift
    bounds in `.tycho.toml`. Positional globs edit the include allowlist, `--exclude` the
    denylist (exclude wins). An empty include leaves scope_drift UNSUPPORTED (zero-config).
    Globs are stored verbatim, so quote them at the shell."""
    from ..store import config as config_mod

    exclude = exclude_globs is not None  # --exclude carries its own globs; its presence is the mode
    globs = exclude_globs if exclude else paths
    if action != "list":
        if not globs:
            flag = " --exclude" if exclude else ""
            print(
                f"tycho scope {action}{flag}: give at least one glob, "
                f"e.g. tycho scope {action}{flag} 'src/**' 'tests/**'",
                file=sys.stderr,
            )
            return ExitCode.USAGE
        include_fns = {"set": config_mod.set_scope, "add": config_mod.add_scope, "remove": config_mod.remove_scope}
        exclude_fns = {"set": config_mod.set_exclude, "add": config_mod.add_exclude, "remove": config_mod.remove_exclude}
        (exclude_fns if exclude else include_fns)[action](cwd, globs)

    cfg = config_mod.load(cwd)
    if cfg.scope_include:
        print(f"scope ({config_mod.CONFIG_NAME}) — the agent may edit:")
        for g in cfg.scope_include:
            print(f"  {g}")
        if cfg.scope_exclude:
            print("  …except (exclude wins):")
            for g in cfg.scope_exclude:
                print(f"    !{g}")
        print("edits outside these FAIL scope_drift.")
    else:
        print("scope: none set — every path is in scope, so scope_drift stays UNSUPPORTED (zero-config).")
        print("set bounds with: tycho scope add 'src/**' 'tests/**'")
        if cfg.scope_exclude:  # exclude without include does nothing — be honest about it
            print(f"note: exclude is set but ignored while include is empty: {', '.join(cfg.scope_exclude)}")
    return ExitCode.OK


def _relay(cwd: Path, on: bool, off: bool) -> int:
    """`tycho relay [--on|--off]` — the opt-in verdict relay, off by default. On, the Stop hook
    feeds a non-VERIFIED verdict back to the agent, bounded by ``TYCHO_RELAY_MAX`` (default 3)
    so it can't loop forever. Bare ``tycho relay`` just reports the setting."""
    from ..store import state

    repo = state.root_for(cwd)
    if on or off:
        state.set_relay_enabled(repo, enabled=on)
    enabled = state.relay_enabled(repo)
    if not (on or off):
        print(f"tycho: verdict relay is {'ON' if enabled else 'OFF'} for {repo}"
              f"{'' if enabled else ' — verdicts stay human-only (no agent context used)'}.")
        print("  toggle: `tycho relay --on` | `--off`   ·   in Claude Code: /tycho-relay-on | "
              "/tycho-relay-off   ·   stored in .tycho.toml [relay].")
    elif enabled:
        print(f"tycho: verdict relay ON for {repo} — the agent now sees a non-VERIFIED verdict and "
              f"keeps working until VERIFIED, up to {state.relay_max()} automatic re-checks per turn. "
              f"This spends extra tokens; turn it back off with `tycho relay --off`.")
    else:
        print(f"tycho: verdict relay OFF for {repo} — verdicts stay human-only, no agent context used.")
    return ExitCode.OK


def _override(cwd: Path, check: str | None, reason: str | None,
              on: bool, off: bool, veto: bool = False, unveto: bool = False) -> int:
    """`tycho override [--on|--off|--veto|--unveto] | <check> "<reason>"` — toggle the
    capability, record a per-check override (agent), or veto one (operator). Off by default;
    overrides and vetoes are logged to .tycho/overrides.json."""
    from ..store import state

    repo = state.root_for(cwd)
    if veto:
        targets = [check] if check else [m["check"] for m in state.overrides(repo)]
        if not targets:
            print("tycho: no active override to veto.")
            return ExitCode.OK
        for t in targets:
            state.veto_override(repo, t)
        print(f"tycho: vetoed {', '.join(targets)} — the override no longer applies and the relay "
              f"will fire again on the next check. Lift it with `tycho override --unveto <check>`.")
        return ExitCode.OK
    if unveto:
        if not check:
            print("tycho: name the check to lift: `tycho override --unveto <check>`.")
            return ExitCode.OK
        state.unveto_override(repo, check)
        print(f"tycho: lifted the veto on {check} — it may be overridden again.")
        return ExitCode.OK
    if on or off:
        state.set_override_enabled(repo, enabled=on)
        enabled = state.override_enabled(repo)
        if enabled:
            print(f"tycho: verdict override ON for {repo} — when the relay is on, the agent may "
                  f"record `tycho override <check> \"<reason>\"`; it becomes OVERRIDDEN (agent-"
                  f"authorized, not proven) and is logged. Turn it off with `tycho override --off`.")
        else:
            print(f"tycho: verdict override OFF for {repo} — the agent cannot override verdicts.")
        return ExitCode.OK
    if check is None:  # bare status
        enabled = state.override_enabled(repo)
        print(f"tycho: verdict override is {'ON' if enabled else 'OFF'} for {repo}.")
        vetoes = state.vetoed(repo)
        if vetoes:
            print(f"  vetoed checks (not overridable): {', '.join(vetoes)} — lift with "
                  f"`tycho override --unveto <check>`.")
        print("  toggle: `tycho override --on` | `--off`   ·   in Claude Code: /tycho-override-on | "
              "/tycho-override-off   ·   stored in .tycho.toml [override].")
        return ExitCode.OK
    # record action
    from ..engine import checks as checks_mod

    known = {c.__name__ for c in checks_mod.CHECKS}
    if not check.strip():
        print("tycho: name the check to override — `tycho override <check> \"<reason>\"`. Nothing recorded.")
        return ExitCode.OK
    if check not in known:
        print(f"tycho: unknown check {check!r}. Valid checks: {', '.join(sorted(known))}. "
              f"Nothing recorded.")
        return ExitCode.OK
    if not state.override_enabled(repo):
        print("tycho: verdict override is off here — enable it with `tycho override --on` "
              "(it stays off by default). Nothing recorded.")
        return ExitCode.OK
    if check in state.vetoed(repo):
        print(f"tycho: {check} was vetoed by the user — fix it or lift the veto "
              f"(`tycho override --unveto {check}`). Nothing recorded.")
        return ExitCode.OK
    if not reason or not reason.strip():
        print("tycho: an override needs a reason — `tycho override <check> \"<why it doesn't apply>\"`. "
              "Nothing recorded.")
        return ExitCode.OK
    state.record_override(repo, check, reason.strip())
    print(f"tycho: recorded override of {check} — \"{reason.strip()}\". It becomes OVERRIDDEN "
          f"(agent-authorized, not proven) if no adverse check survives, and is logged.")
    return ExitCode.OK

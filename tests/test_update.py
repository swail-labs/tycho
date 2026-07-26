"""Update check + `tycho update` (TYCHO-53/10).

The check is best-effort and offline-safe: it never raises, never blocks, hits the network at
most once a day (cached machine-wide), and stays silent when it can't tell. Every test opts the
suite out of the network by default (conftest sets TYCHO_NO_UPDATE_CHECK=1); these delete it and
monkeypatch the fetch.
"""

from __future__ import annotations

import time

import pytest

import tycho
from tycho import cli, state
from tycho import version as version_mod


@pytest.fixture
def _online(monkeypatch):
    """Re-enable the check (conftest opts out) with a stubbed network fetch."""
    monkeypatch.delenv("TYCHO_NO_UPDATE_CHECK", raising=False)


def _fetches(monkeypatch, value):
    monkeypatch.setattr(version_mod, "_fetch", lambda: value)


# --- version comparison ------------------------------------------------------

def test_is_newer_compares_release_tuples():
    assert version_mod.is_newer("0.1.0", "0.0.1")
    assert version_mod.is_newer("0.0.2", "0.0.1")
    assert not version_mod.is_newer("0.0.1", "0.0.1")
    assert not version_mod.is_newer("0.0.1", "0.1.0")


# --- the distribution name we check against ----------------------------------

def test_checks_the_tycho_cli_distribution_not_the_taken_tycho_name():
    # `tycho` is taken on PyPI by an unrelated project; we publish/check as `tycho-cli` (TYCHO-73).
    assert version_mod._DIST_NAME == "tycho-cli"
    assert version_mod._index_url() == "https://pypi.org/pypi/tycho-cli/json"


def test_fetch_is_inert_without_a_name(monkeypatch):
    # Setting the name to None disables the check entirely (no network, no notice).
    monkeypatch.setattr(version_mod, "_DIST_NAME", None)
    assert version_mod._index_url() is None
    assert version_mod._fetch() is None  # returns before any network call


# --- the check ---------------------------------------------------------------

def test_notice_when_a_newer_version_exists(_online, monkeypatch):
    _fetches(monkeypatch, "9.9.9")
    note = version_mod.notice(refresh_first=True)
    assert note and "9.9.9" in note and tycho.__version__ in note


def test_no_notice_when_up_to_date(_online, monkeypatch):
    _fetches(monkeypatch, tycho.__version__)
    assert version_mod.notice(refresh_first=True) is None


def test_opt_out_env_silences_the_notice(monkeypatch):
    # conftest sets TYCHO_NO_UPDATE_CHECK=1; even a newer version stays silent.
    monkeypatch.setattr(version_mod, "_fetch", lambda: "9.9.9")
    assert version_mod.notice(refresh_first=True) is None


def test_offline_is_silent_not_an_error(_online, monkeypatch):
    _fetches(monkeypatch, None)  # fetch failed / offline
    assert version_mod.notice(refresh_first=True) is None


def test_result_is_cached_and_not_refetched(_online, monkeypatch):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return "9.9.9"

    monkeypatch.setattr(version_mod, "_fetch", counting)
    assert version_mod.refresh() == "9.9.9"
    assert version_mod.refresh() == "9.9.9"  # cache is fresh → no second fetch
    assert calls["n"] == 1


def test_stale_cache_triggers_a_refetch(_online, monkeypatch):
    state.write_update_cache(latest="1.0.0", checked_at=time.time() - 999999)  # a day+ old
    _fetches(monkeypatch, "2.0.0")
    assert version_mod.refresh() == "2.0.0"


def test_force_bypasses_a_fresh_cache(_online, monkeypatch):
    # The bug: a same-day release is invisible because the ≤24h cache short-circuits refresh().
    # `tycho update`/`doctor` pass force=True to re-hit the index now (TYCHO-53).
    state.write_update_cache(latest="0.0.2", checked_at=time.time())  # fresh, but stale content
    _fetches(monkeypatch, "0.0.3")
    assert version_mod.refresh() == "0.0.2"            # cached path still honors the day
    assert version_mod.refresh(force=True) == "0.0.3"  # forced path sees the new release


def test_status_bar_path_reads_cache_only_never_network(_online, monkeypatch):
    # refresh_first=False must not fetch — the status bar renders constantly.
    monkeypatch.setattr(version_mod, "_fetch", lambda: pytest.fail("must not hit the network"))
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    assert "9.9.9" in version_mod.notice(refresh_first=False)


# --- dismissal ---------------------------------------------------------------

def test_dismiss_silences_that_version_and_counts(_online, monkeypatch):
    _fetches(monkeypatch, "9.9.9")
    assert version_mod.notice(refresh_first=True)          # shown first
    state.dismiss_update("9.9.9")
    assert version_mod.notice(refresh_first=True) is None  # dismissed → silent
    assert state.update_dismissed_count() == 1


def test_dismissing_one_version_still_notifies_for_the_next(_online, monkeypatch):
    _fetches(monkeypatch, "9.9.9")
    state.dismiss_update("9.9.9")
    _fetches(monkeypatch, "9.9.10")
    state.write_update_cache(checked_at=0)  # force a refetch
    assert "9.9.10" in version_mod.notice(refresh_first=True)


def test_notice_never_raises(monkeypatch):
    monkeypatch.setattr(version_mod, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.delenv("TYCHO_NO_UPDATE_CHECK", raising=False)
    assert version_mod.notice(refresh_first=True) is None  # swallowed


# --- the command -------------------------------------------------------------

def test_update_reports_up_to_date(_online, monkeypatch, capsys):
    _fetches(monkeypatch, tycho.__version__)
    assert cli.main(["update"]) == cli.ExitCode.OK
    assert "up to date" in capsys.readouterr().out


def test_update_offline_is_reported_not_fatal(_online, monkeypatch, capsys):
    _fetches(monkeypatch, None)
    assert cli.main(["update"]) == cli.ExitCode.OK
    assert "couldn't reach" in capsys.readouterr().out


def test_update_prints_the_upgrade_command_then_runs_it(_online, monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr(cli.sys, "platform", "linux")  # POSIX runs the upgrade in place
    _fetches(monkeypatch, "9.9.9")
    monkeypatch.setattr(cli, "_upgrade_command", lambda force=False: ["echo", "upgrading"])
    ran = {}

    class _Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: ran.setdefault("cmd", cmd) or _Done())
    assert cli.main(["update"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "9.9.9" in out and "echo upgrading" in out
    assert ran["cmd"] == ["echo", "upgrading"]


def test_update_on_windows_defers_the_upgrade_past_process_exit(_online, monkeypatch, capsys):
    # A running .exe can't have its own shim replaced on Windows, so the upgrade must be deferred
    # to a detached child that waits for us to exit — never run synchronously (TYCHO-108 follow-up).
    import subprocess

    monkeypatch.setattr(cli.sys, "platform", "win32")
    _fetches(monkeypatch, "9.9.9")
    monkeypatch.setattr(cli, "_upgrade_command", lambda force=False: ["uv", "tool", "upgrade", "tycho-cli"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not upgrade in-process on Windows"))
    spawned = {}
    monkeypatch.setattr(cli, "_spawn_deferred_upgrade", lambda cmd: spawned.setdefault("cmd", list(cmd)))
    assert cli.main(["update"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "9.9.9" in out and "once this process exits" in out
    assert spawned["cmd"] == ["uv", "tool", "upgrade", "tycho-cli"]


@pytest.mark.parametrize("prefix", ["/home/u/.local/pipx/venvs/tycho-cli",
                                    "/home/u/.local/share/uv/tools/tycho-cli",
                                    "/usr"])  # pipx, uv, plain-pip branches
def test_upgrade_command_names_the_distribution_not_the_taken_tycho(monkeypatch, prefix):
    # Must upgrade `tycho-cli`, never `tycho` — the bare name is an unrelated PyPI project, so
    # `pip install --upgrade tycho` would pull that and `pipx/uv upgrade tycho` wouldn't resolve
    # the installed tool (TYCHO-96).
    monkeypatch.setattr(cli.sys, "prefix", prefix)
    cmd = cli._upgrade_command()
    assert "tycho-cli" in " ".join(cmd)
    assert "tycho" not in cmd  # the bare command name must never be a standalone upgrade target


def test_upgrade_command_npm_channel_overrides_prefix(monkeypatch):
    # The npm wrapper sets TYCHO_INSTALL=npm before exec'ing the frozen binary. That binary has no
    # pipx/uv/pip prefix, so without this it falls through to a `pip install` it can't run (no
    # bundled pip in a PyInstaller build). npm owns its upgrade: reinstall the global package, and
    # the channel signal must win over any incidental sys.prefix (TYCHO-106).
    monkeypatch.setenv("TYCHO_INSTALL", "npm")
    monkeypatch.setattr(cli.sys, "prefix", "/usr")  # would otherwise be the plain-pip branch
    assert cli._upgrade_command() == ["npm", "install", "-g", "@swail-labs/tycho@latest"]


@pytest.mark.parametrize("executable", ["/opt/homebrew/Cellar/tycho/0.1.0/bin/tycho",
                                        "/home/linuxbrew/.linuxbrew/Cellar/tycho/0.1.0/bin/tycho"])
def test_upgrade_command_detects_a_homebrew_binary_from_its_path(monkeypatch, executable):
    # The formula installs a bare binary — no wrapper to set TYCHO_INSTALL the way npm does — so
    # the channel comes from the Cellar path. Without this it falls through to a `pip install`
    # the frozen binary can't run (TYCHO-105).
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "executable", executable)
    monkeypatch.setattr(cli.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(cli.sys, "prefix", "/usr")  # would otherwise be the plain-pip branch

    assert cli._upgrade_command() == ["brew", "upgrade", "swail-labs/tap/tycho"]
    assert cli._upgrade_command(force=True) == ["brew", "reinstall", "swail-labs/tap/tycho"]


def test_pip_install_into_homebrews_python_is_not_the_brew_channel(monkeypatch):
    # `pip install tycho-cli` under brew's Python lives in a Cellar path too, but upgrades with
    # pip — only a *frozen* binary in the Cellar came from the tap.
    monkeypatch.setattr(cli.sys, "executable", "/opt/homebrew/Cellar/python@3.12/3.12.8/bin/python3.12")
    monkeypatch.setattr(cli.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(cli.sys, "prefix", "/opt/homebrew/Cellar/python@3.12/3.12.8")

    assert cli._is_homebrew_install() is False
    assert "brew" not in cli._upgrade_command()


@pytest.mark.parametrize("prefix", ["/home/u/.local/pipx/venvs/tycho-cli",
                                    "/home/u/.local/share/uv/tools/tycho-cli"])  # pip has no persisted pin
def test_force_crosses_a_version_pin_plain_respects_it(monkeypatch, prefix):
    # A user who installed `tycho-cli==X` on purpose: plain `update` must stay within that pin
    # (`upgrade`), and only `--force` reinstalls the latest across it (`install …@latest`/`--force`).
    monkeypatch.setattr(cli.sys, "prefix", prefix)
    assert "upgrade" in cli._upgrade_command(force=False)
    assert "install" in cli._upgrade_command(force=True)


def test_update_skip_dismisses_and_counts(_online, monkeypatch, capsys):
    _fetches(monkeypatch, "9.9.9")
    assert cli.main(["update", "--skip"]) == cli.ExitCode.OK
    assert "dismissed" in capsys.readouterr().out
    assert state.update_dismissed_count() == 1


# --- SessionStart bootup notice ----------------------------------------------

def _stdin(monkeypatch, text="{}"):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_session_start_emits_systemmessage_when_behind(_online, monkeypatch, capsys):
    import json

    from tycho import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch)
    assert hook.session_start() == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert msg.startswith("Tycho:") and "9.9.9" in msg  # user-facing, never additionalContext


def test_session_start_uses_opencode_message_field(_online, monkeypatch, capsys):
    # OpenCode's plugin reads `.message` and toasts it — the notice must match that shape,
    # not Claude's `systemMessage` (TYCHO-72).
    import json

    from tycho import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch, '{"harness": "opencode", "sessionID": "s1"}')
    assert hook.session_start() == 0
    out = json.loads(capsys.readouterr().out)
    assert "systemMessage" not in out
    assert out["message"].startswith("Tycho:") and "9.9.9" in out["message"]


def test_session_start_is_silent_on_cursor_no_human_channel(_online, monkeypatch, capsys):
    # Cursor has no human-only sink (notice_output is None) — a notice there would be
    # model-facing, which the TYCHO-35 rule forbids, so it emits nothing (TYCHO-72/112).
    from tycho import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch, '{"workspace_roots": ["/tmp/x"], "cursor_version": "1"}')
    assert hook.session_start() == 0
    assert capsys.readouterr().out == ""


def test_session_start_is_silent_when_up_to_date(_online, monkeypatch, capsys):
    from tycho import hook

    _fetches(monkeypatch, tycho.__version__)
    _stdin(monkeypatch)
    assert hook.session_start() == 0
    assert capsys.readouterr().out == ""


def test_session_start_never_raises_and_prints_nothing_on_error(monkeypatch, capsys):
    from tycho import hook

    monkeypatch.delenv("TYCHO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(version_mod, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    _stdin(monkeypatch)
    assert hook.session_start() == 0  # a bootup hook must never break the session
    assert capsys.readouterr().out == ""


# --- Stop-hook update notice (TYCHO-116) -------------------------------------
#
# The Stop hook appends a human-only "newer Tycho available" line to the verdict the user is
# already reading — cache-only (no network on the hot path), never in the model-facing
# additionalContext, and suppressed where there's no human-only channel (Cursor).

from pathlib import Path as _Path  # noqa: E402

_CLAUDE_FIXTURE = _Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def _claude_stop(repo) -> str:
    import json
    return json.dumps({"cwd": str(repo), "transcript_path": str(_CLAUDE_FIXTURE.resolve())})


def test_stop_hook_appends_update_line_to_human_output(_online, tmp_path):
    from tycho import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert out is not None and "9.9.9" in out["systemMessage"]  # verdict + update line, together


def test_stop_hook_update_line_is_never_model_facing(_online, tmp_path):
    # With the relay on there IS an additionalContext (model-facing) copy — the update line must
    # ride only the human systemMessage, never that (TYCHO-35: don't tell the model to self-update).
    from tycho import hook
    state.set_relay_enabled(tmp_path, True)
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" in out["systemMessage"]
    assert "9.9.9" not in out["hookSpecificOutput"]["additionalContext"]


def test_stop_hook_silent_when_up_to_date(_online, tmp_path):
    from tycho import hook
    state.write_update_cache(latest=tycho.__version__, checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "newer Tycho" not in out["systemMessage"]


def test_stop_hook_respects_dismissal(_online, tmp_path):
    from tycho import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    state.dismiss_update("9.9.9")  # `tycho update --skip` waved this version off
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" not in out["systemMessage"]


def test_stop_hook_silent_when_opted_out(tmp_path):
    # conftest sets TYCHO_NO_UPDATE_CHECK=1; even a fresh cache with a newer version stays silent.
    from tycho import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert out is not None and "9.9.9" not in out["systemMessage"]


def test_stop_hook_update_notice_never_hits_the_network(_online, monkeypatch, tmp_path):
    from tycho import hook
    monkeypatch.setattr(version_mod, "_fetch", lambda: pytest.fail("Stop path must not hit the network"))
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" in out["systemMessage"]  # served from the cache only


def test_stop_hook_suffix_suppressed_without_a_human_channel(_online):
    # Cursor: format_output is model-facing and notice_output is None, so a notice would reach the
    # model — suppress it, exactly as the bootup notice does (TYCHO-35/72).
    from types import SimpleNamespace

    from tycho import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    assert hook._update_suffix(SimpleNamespace(notice_output=None)) == ""

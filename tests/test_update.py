"""Update check + `tycho update`.

The check is best-effort and offline-safe: it never raises, never blocks, hits the network at
most once a day (cached machine-wide), and stays silent when it can't tell. Every test opts the
suite out of the network by default (conftest sets TYCHO_NO_UPDATE_CHECK=1); these delete it and
monkeypatch the fetch.
"""

from __future__ import annotations

import time

import pytest

import tycho
from tycho import cli
from tycho.store import state
from tycho.wire import version as version_mod


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
    # `tycho` is taken on PyPI by an unrelated project; we publish/check as `tycho-cli`.
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
    # `tycho update`/`doctor` pass force=True to re-hit the index now.
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
    # to a detached child that waits for us to exit — never run synchronously ( follow-up).
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
    # `tycho-cli`, never `tycho` — the bare name is an unrelated PyPI project.
    monkeypatch.setattr(cli.sys, "prefix", prefix)
    cmd = cli._upgrade_command()
    assert "tycho-cli" in " ".join(cmd)
    assert "tycho" not in cmd  # the bare command name must never be a standalone upgrade target


def test_upgrade_command_npm_channel_overrides_prefix(monkeypatch):
    # The npm wrapper sets TYCHO_INSTALL before exec'ing the frozen binary, which has no
    # pipx/uv/pip prefix and no bundled pip. The channel signal must beat any incidental
    # sys.prefix, or it falls through to a `pip install` it can't run.
    monkeypatch.setenv("TYCHO_INSTALL", "npm")
    monkeypatch.setattr(cli.sys, "prefix", "/usr")  # would otherwise be the plain-pip branch
    assert cli._upgrade_command() == ["npm", "install", "-g", "@swail-labs/tycho@latest"]


@pytest.mark.parametrize("executable", ["/opt/homebrew/Cellar/tycho/0.1.0/bin/tycho",
                                        "/home/linuxbrew/.linuxbrew/Cellar/tycho/0.1.0/bin/tycho"])
def test_upgrade_command_detects_a_homebrew_binary_from_its_path(monkeypatch, executable):
    # A bare binary with no wrapper to set TYCHO_INSTALL, so the channel comes from the
    # Cellar path — else it falls through to a `pip install` the frozen binary can't run.
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


def _frozen_at(monkeypatch, executable, platform="darwin"):
    """A standalone binary installed by install.sh — no npm wrapper, not in a Cellar."""
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "executable", executable)
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(cli.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(cli.sys, "prefix", "/tmp/_MEIabc123")  # PyInstaller's unpack dir
    monkeypatch.delenv("TYCHO_INSTALL", raising=False)


def test_curl_installed_binary_reinstalls_itself_instead_of_calling_pip(monkeypatch):
    # The regression this exists for: a frozen binary that is neither npm nor Homebrew used to
    # fall through to the pip default, producing `<the binary> -m pip install --upgrade tycho-cli`
    # — the binary handed pip's arguments, which a PyInstaller build cannot serve. install.sh is
    # the README's headline install, so this was the most-used channel's update path.
    _frozen_at(monkeypatch, "/home/u/.local/bin/tycho")

    cmd = cli._upgrade_command()
    assert cmd[:2] == ["sh", "-c"]
    assert "install.sh" in cmd[2]
    assert "-m" not in cmd and "pip" not in cmd


def test_reinstall_targets_the_directory_the_binary_already_lives_in(monkeypatch):
    # TYCHO_INSTALL_DIR lets the binary live anywhere. Re-running the installer without pinning
    # the dir would drop a fresh copy in ~/.local/bin and leave the one on PATH stale — an
    # "upgrade" that reports success and changes nothing.
    _frozen_at(monkeypatch, "/opt/tools/bin/tycho")

    assert "TYCHO_INSTALL_DIR=/opt/tools/bin" in cli._upgrade_command()[2]


def test_reinstall_quotes_an_install_dir_containing_spaces(monkeypatch):
    # The dir is interpolated into a shell command line; an unquoted space would split it into
    # two words and install somewhere unintended.
    _frozen_at(monkeypatch, "/Users/u/My Tools/tycho")

    assert "TYCHO_INSTALL_DIR='/Users/u/My Tools'" in cli._upgrade_command()[2]


def test_direct_channel_can_be_forced_by_env(monkeypatch):
    # Mirrors TYCHO_INSTALL=brew: an escape hatch for an install the detection can't see.
    monkeypatch.setenv("TYCHO_INSTALL", "direct")
    monkeypatch.setattr(cli.sys, "prefix", "/usr")  # would otherwise be the plain-pip branch
    monkeypatch.setattr(cli.os.path, "realpath", lambda p: "/home/u/.local/bin/tycho")

    assert cli._upgrade_command()[:2] == ["sh", "-c"]


def test_frozen_windows_binary_keeps_the_old_fallback(monkeypatch):
    # install.sh is POSIX-only — there is no installer to re-run on Windows, where the npm
    # wrapper is the supported standalone channel and sets TYCHO_INSTALL itself.
    _frozen_at(monkeypatch, r"C:/Users/u/tycho.exe", platform="win32")

    assert cli._upgrade_command()[:1] != ["sh"]


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

    from tycho.wire import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch)
    assert hook.session_start() == 0
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert msg.startswith("Tycho:") and "9.9.9" in msg  # user-facing, never additionalContext


def test_session_start_uses_opencode_message_field(_online, monkeypatch, capsys):
    # OpenCode's plugin reads `.message` and toasts it — the notice must match that shape,
    # not Claude's `systemMessage`.
    import json

    from tycho.wire import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch, '{"harness": "opencode", "sessionID": "s1"}')
    assert hook.session_start() == 0
    out = json.loads(capsys.readouterr().out)
    assert "systemMessage" not in out
    assert out["message"].startswith("Tycho:") and "9.9.9" in out["message"]


def test_session_start_is_silent_on_cursor_no_human_channel(_online, monkeypatch, capsys):
    # Cursor has no human-only sink (notice_output is None) — a notice there would be
    # model-facing, which the rule forbids, so it emits nothing.
    from tycho.wire import hook

    _fetches(monkeypatch, "9.9.9")
    _stdin(monkeypatch, '{"workspace_roots": ["/tmp/x"], "cursor_version": "1"}')
    assert hook.session_start() == 0
    assert capsys.readouterr().out == ""


def test_session_start_is_silent_when_up_to_date(_online, monkeypatch, capsys):
    from tycho.wire import hook

    _fetches(monkeypatch, tycho.__version__)
    _stdin(monkeypatch)
    assert hook.session_start() == 0
    assert capsys.readouterr().out == ""


def test_session_start_never_raises_and_prints_nothing_on_error(monkeypatch, capsys):
    from tycho.wire import hook

    monkeypatch.delenv("TYCHO_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(version_mod, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    _stdin(monkeypatch)
    assert hook.session_start() == 0  # a bootup hook must never break the session
    assert capsys.readouterr().out == ""


# --- Stop-hook update notice -------------------------------------
# Appended to the verdict the user is already reading: cache-only (no network on the hot
# path), never model-facing, suppressed where there is no human-only channel.

from pathlib import Path as _Path  # noqa: E402

_CLAUDE_FIXTURE = _Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def _claude_stop(repo) -> str:
    import json
    return json.dumps({"cwd": str(repo), "transcript_path": str(_CLAUDE_FIXTURE.resolve())})


def test_stop_hook_appends_update_line_to_human_output(_online, tmp_path):
    from tycho.wire import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert out is not None and "9.9.9" in out["systemMessage"]  # verdict + update line, together


def test_stop_hook_update_line_is_never_model_facing(_online, tmp_path):
    # With the relay on there IS an additionalContext (model-facing) copy — the update line must
    # ride only the human systemMessage, never that (: don't tell the model to self-update).
    from tycho.wire import hook
    state.set_relay_enabled(tmp_path, True)
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" in out["systemMessage"]
    assert "9.9.9" not in out["hookSpecificOutput"]["additionalContext"]


def test_stop_hook_silent_when_up_to_date(_online, tmp_path):
    from tycho.wire import hook
    state.write_update_cache(latest=tycho.__version__, checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "newer Tycho" not in out["systemMessage"]


def test_stop_hook_respects_dismissal(_online, tmp_path):
    from tycho.wire import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    state.dismiss_update("9.9.9")  # `tycho update --skip` waved this version off
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" not in out["systemMessage"]


def test_stop_hook_silent_when_opted_out(tmp_path):
    # conftest sets TYCHO_NO_UPDATE_CHECK=1; even a fresh cache with a newer version stays silent.
    from tycho.wire import hook
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert out is not None and "9.9.9" not in out["systemMessage"]


def test_stop_hook_update_notice_never_hits_the_network(_online, monkeypatch, tmp_path):
    from tycho.wire import hook
    monkeypatch.setattr(version_mod, "_fetch", lambda: pytest.fail("Stop path must not hit the network"))
    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    out = hook.run(_claude_stop(tmp_path))
    assert "9.9.9" in out["systemMessage"]  # served from the cache only


def test_stop_hook_notice_carries_a_hands_off_line_on_a_shared_channel(_online):
    """The user hears about an update on every harness; the model is told not to act on it.

    Withholding it entirely was the earlier answer, and it meant Codex users were never told a
    newer Tycho existed. The risk being managed is narrow and real — an agent handed "0.2.2 is
    available" may go install it, and a verifier that rewrites itself mid-verification on its own
    advice is the update nobody wanted — so the mitigation travels with the notice instead.
    """
    import tempfile
    from pathlib import Path

    from tycho.read import harness as harness_mod
    from tycho.wire import hook

    state.write_update_cache(latest="9.9.9", checked_at=time.time())
    repo = Path(tempfile.mkdtemp())
    # Claude: a free channel, so the notice goes out bare.
    claude = hook._update_suffix(repo, harness_mod.CLAUDE)
    assert "9.9.9" in claude
    assert "not a task" not in claude  # nothing to warn: the model never sees this
    # Codex and Cursor: the user still hears about it, and the model is told to leave it alone.
    for name in ("codex", "cursor"):
        got = hook._update_suffix(repo, harness_mod.BY_NAME[name])
        assert "9.9.9" in got, name
        assert "do not install, upgrade or configure Tycho" in got, name
    # A harness that can reach nobody says nothing at all.
    class _Mute:
        channels = harness_mod.Channels(
            human_only=False, model_only=True, shared=False, relays=False
        )
    assert hook._update_suffix(repo, _Mute) == ""

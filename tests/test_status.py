"""TYCHO-39 — the passive indicator: can the user see Tycho is on without asking?

Two things are worth guarding here, and neither is the pretty output.

The first is that the line never *overclaims*. It renders in the corner of someone's
terminal all day; if it shows a green tick while the hook is dead, or keeps yesterday's
VERIFIED after a run that verified nothing, it is worse than no indicator at all — it's
the silent-trust failure `doctor` exists to catch, dressed up as reassurance.

The second is that it can't hurt anything. It runs on every render, inside a harness that
gives it ~5s and reads stdout only on exit 0 (Claude Code 2.1.210 — see
`docs/harness-support.md`). So: never raise, never block, never clobber the user's own
status line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tycho import cli, state, status
from tycho import init as init_mod

CLAUDE = Path(".claude/settings.json")


@pytest.fixture(autouse=True)
def _no_colour(monkeypatch):
    """Assert on text, not escape codes. Colour has its own test."""
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """No real $HOME: composition reads user-level ~/.claude, and this machine's has a
    a third-party statusLine — without this, every _install() would record it as a wrap."""
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: empty / f".{name}")


def _install(repo: Path) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    init_mod.init(repo, only="claude", assume_yes=True)


def _boom(*a, **k):
    raise RuntimeError("disk gone")


# --- what the line says ------------------------------------------------------

def test_a_repo_without_tycho_shows_nothing(tmp_path: Path):
    # Not installed here: stay out of the user's status bar entirely.
    assert status.line(tmp_path) == ""


def test_the_line_is_just_bracket_tycho(tmp_path: Path):
    # TYCHO-47: text is always `[TYCHO]`; the *colour* carries the status, not the text.
    # (NO_COLOR from the fixture strips the codes, so the text shows bare here.)
    _install(tmp_path)
    assert status.line(tmp_path) == "[TYCHO]"                 # never fired → still [TYCHO]
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    assert status.line(tmp_path) == "[TYCHO]"
    state.record_run(tmp_path, "claude", verdict="FAILED")
    assert status.line(tmp_path) == "[TYCHO]"


def test_colour_tracks_the_verdict(tmp_path: Path, monkeypatch):
    # green = VERIFIED, red = FAILED/STALE (both adverse), frost blue = verifying (pending),
    # grey = never-fired / no-verdict. The badge lands on green or red (TYCHO-47/59/94).
    monkeypatch.delenv("NO_COLOR", raising=False)
    _install(tmp_path)

    assert status.line(tmp_path).startswith(status._GREY)     # installed, never fired — grey
    state.record_run(tmp_path, "claude", pending=True)
    assert status.line(tmp_path).startswith(status._FROST)    # verifying now — frost blue (TYCHO-94)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    assert status.line(tmp_path).startswith(status._GREEN)
    state.record_run(tmp_path, "claude", verdict="FAILED")
    assert status.line(tmp_path).startswith(status._RED)
    state.record_run(tmp_path, "claude", verdict="STALE")
    assert status.line(tmp_path).startswith(status._RED)      # STALE is adverse → red


def test_pending_is_frost_not_yellow(tmp_path: Path, monkeypatch):
    # mid-run and INDETERMINATE must be distinct colours now — frost blue vs yellow (TYCHO-94)
    monkeypatch.delenv("NO_COLOR", raising=False)
    _install(tmp_path)
    state.record_run(tmp_path, "claude", pending=True)
    assert status.line(tmp_path).startswith(status._FROST)
    assert status._FROST != status._YELLOW


def test_unsupported_and_nothing_to_verify_are_grey(tmp_path: Path, monkeypatch):
    # UNSUPPORTED (nothing this run could check) and a completed run with no verdict are
    # grey — not green, and not frost/yellow (reserved for in-flight / INDETERMINATE).
    monkeypatch.delenv("NO_COLOR", raising=False)
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="UNSUPPORTED")
    assert status.line(tmp_path).startswith(status._GREY)
    state.record_run(tmp_path, "claude")  # fired, nothing to report (no verdict, not pending)
    assert status.line(tmp_path).startswith(status._GREY)


def test_indeterminate_is_yellow(tmp_path: Path, monkeypatch):
    # INDETERMINATE ran but couldn't conclude — noteworthy, so yellow, distinct from frost (TYCHO-94)
    monkeypatch.delenv("NO_COLOR", raising=False)
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="INDETERMINATE")
    assert status.line(tmp_path).startswith(status._YELLOW)


def test_a_run_that_verified_nothing_is_not_green(tmp_path: Path, monkeypatch):
    # The regression that would make this a liar: yesterday's VERIFIED (green) surviving a
    # run that proved nothing today. A no-verdict run must drop off green (to grey).
    monkeypatch.delenv("NO_COLOR", raising=False)
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    state.record_run(tmp_path, "claude")
    assert not status.line(tmp_path).startswith(status._GREEN)  # not green anymore
    assert status.line(tmp_path).startswith(status._GREY)       # grey — nothing to report


# --- refresh cadence so the badge settles on the verdict (TYCHO-59) ----------

def test_install_sets_a_status_refresh_interval(tmp_path: Path):
    # Without polling the badge lags to the next prompt; the interval makes it re-render a
    # beat after the Stop event, once the verdict is written.
    _install(tmp_path)
    data = json.loads((tmp_path / CLAUDE).read_text())
    assert data["statusLine"]["refreshInterval"] == init_mod._STATUS_REFRESH_MS


def test_a_user_set_refresh_interval_is_preserved(tmp_path: Path):
    _install(tmp_path)
    settings = tmp_path / CLAUDE
    data = json.loads(settings.read_text())
    data["statusLine"]["refreshInterval"] = 5000  # the user tunes it
    settings.write_text(json.dumps(data))
    _install(tmp_path)  # re-init must not stomp it
    assert json.loads(settings.read_text())["statusLine"]["refreshInterval"] == 5000


# --- how the harness talks to it ---------------------------------------------

def test_the_project_root_wins_over_cwd(tmp_path: Path):
    # `cwd` follows the user into subdirectories; `.tycho/` lives at the root.
    payload = {"cwd": str(tmp_path / "sub"), "workspace": {"project_dir": str(tmp_path)}}
    assert status.repo_of(payload) == tmp_path


def test_cwd_is_used_when_there_is_no_workspace(tmp_path: Path):
    assert status.repo_of({"cwd": str(tmp_path)}) == tmp_path


def test_a_junk_payload_falls_back_rather_than_raising(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert status.repo_of({}) == Path.cwd()
    assert status.repo_of({"workspace": "not-a-dict", "cwd": None}) == Path.cwd()


# --- it must never hurt anything ---------------------------------------------

def test_cli_status_exits_zero_and_says_nothing_on_a_bare_repo(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)

    assert cli.main(["status"]) == cli.ExitCode.OK
    assert capsys.readouterr().out == ""


def test_cli_status_survives_unreadable_state(tmp_path: Path, monkeypatch, capsys):
    # Exit 0 with empty output is the fail-open: the harness then renders nothing.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(status.state, "read_install", _boom)

    assert cli.main(["status"]) == cli.ExitCode.OK
    assert capsys.readouterr().out == ""


def test_cli_status_survives_a_console_that_cannot_encode_it(tmp_path: Path, monkeypatch):
    # TYCHO-40's crash, in the one place it would be worst: a status bar that raises on
    # every render. A non-zero exit also makes the harness discard stdout entirely.
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(status.sys.stdout, "reconfigure", _boom, raising=False)
    monkeypatch.setattr(status.sys.stdout, "write", _cp1252_write, raising=False)

    assert cli.main(["status"]) == cli.ExitCode.OK


def _cp1252_write(text: str) -> int:
    text.encode("cp1252")  # what a Windows console does to "⬤"
    return len(text)


# --- toggle on/off (TYCHO-47) ------------------------------------------------

def test_toggle_off_hides_the_line_but_keeps_the_install(tmp_path: Path):
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    assert status.line(tmp_path) == "[TYCHO]"       # NO_COLOR from the fixture
    state.set_status_enabled(tmp_path, False)
    assert status.line(tmp_path) == ""              # hidden...
    assert state.read_install(tmp_path)             # ...but the hook is still installed
    state.set_status_enabled(tmp_path, True)
    assert status.line(tmp_path) == "[TYCHO]"       # and back


def test_env_override_hides_it_everywhere(tmp_path: Path, monkeypatch):
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    monkeypatch.setenv("TYCHO_STATUS", "off")
    assert status.line(tmp_path) == ""


def test_cli_status_off_then_on_toggles_the_repo(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(status.sys.stdin, "isatty", lambda: True)  # human, not the harness
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")

    assert cli.main(["status", "--off"]) == cli.ExitCode.OK
    assert "hidden" in capsys.readouterr().out
    assert status.line(tmp_path) == ""

    assert cli.main(["status", "--on"]) == cli.ExitCode.OK
    assert "shown" in capsys.readouterr().out
    assert status.line(tmp_path) != ""


def test_verify_records_its_verdict_for_the_status_bar(tmp_path: Path, monkeypatch, capsys):
    # A manual `tycho verify` is a real verification — the badge must reflect it, so a
    # verify → VERIFIED can turn [TYCHO] green (TYCHO-47), same channel the hook writes.
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"

    cli.main(["verify", "--session", str(fixture)])

    out = capsys.readouterr().out
    beat = state.last_run(tmp_path)
    assert beat is not None and "verdict" in beat
    assert beat["verdict"] in out  # the badge shows the same verdict verify just printed


# --- /tycho slash command (TYCHO-48) -----------------------------------------

def test_init_installs_the_slash_command_and_subcommands(tmp_path: Path):
    _install(tmp_path)
    commands = tmp_path / ".claude" / "commands"
    top = commands / "tycho.md"
    assert top.exists() and "$ARGUMENTS" in top.read_text()   # /tycho <freeform>
    # One flat file per subcommand (/tycho-status etc.) → each autocompletes with its own
    # description shown in Claude Code's interface.
    for name in ("status", "doctor", "verify", "help", "hide", "show", "count"):
        sub = commands / f"tycho-{name}.md"
        assert sub.exists(), name
        body = sub.read_text()
        assert body.startswith("---\n")               # frontmatter must lead, or it's not parsed
        assert 'description: "' in body               # quoted, so a colon in the text is safe
    # The doctor description has a colon — the exact case that broke YAML unquoted.
    assert 'description: "Full diagnostics:' in (commands / "tycho-doctor.md").read_text()


def test_uninstall_removes_the_slash_commands(tmp_path: Path):
    _install(tmp_path)
    commands = tmp_path / ".claude" / "commands"
    assert (commands / "tycho-status.md").exists()
    init_mod.uninstall(tmp_path, only="claude")
    assert not commands.exists()  # ours were the only ones — the dir is tidied away


def test_reinit_migrates_the_old_namespaced_layout(tmp_path: Path):
    # Someone on the previous `/tycho:status` layout re-inits: the stale `tycho/` files
    # (ours) are cleaned up, not left orphaned beside the new flat ones.
    commands = tmp_path / ".claude" / "commands"
    old = commands / "tycho" / "status.md"
    old.parent.mkdir(parents=True)
    old.write_text(init_mod._SLASH_MARKER + "\nold\n")  # marked as ours

    _install(tmp_path)

    assert not old.exists() and not (commands / "tycho").exists()  # old layout gone
    assert (commands / "tycho-status.md").exists()                 # new layout present


def test_init_leaves_a_handwritten_tycho_command_alone(tmp_path: Path):
    cmd = tmp_path / ".claude" / "commands" / "tycho.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("# my own /tycho command\n")

    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)

    assert cmd.read_text() == "# my own /tycho command\n"        # untouched
    assert "left your own command file(s) alone: tycho.md" in "\n".join(lines)
    init_mod.uninstall(tmp_path, only="claude")
    assert cmd.exists()                                          # uninstall spares it too


# --- the single statusLine slot ----------------------------------------------

def test_init_claims_the_slot_when_it_is_free(tmp_path: Path):
    _install(tmp_path)
    settings = json.loads((tmp_path / CLAUDE).read_text())
    assert settings["statusLine"]["type"] == "command"
    assert settings["statusLine"]["command"].endswith("statusline")


def test_init_composes_with_a_repo_level_statusline_and_restores_it(tmp_path: Path):
    # A foreign repo-level statusLine is not clobbered — Tycho takes the slot but records
    # their command and runs it too, then puts it back on uninstall (TYCHO-47).
    (tmp_path / ".claude").mkdir()
    theirs = {"type": "command", "command": "~/.claude/other-statusline.sh"}
    (tmp_path / CLAUDE).write_text(json.dumps({"statusLine": theirs}))

    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)

    assert "composing with your existing status line" in "\n".join(lines)
    assert json.loads((tmp_path / CLAUDE).read_text())["statusLine"]["command"].endswith("statusline")
    wrap = state.read_statusline_wrap(tmp_path)
    assert wrap["command"] == theirs["command"] and wrap["origin"] == "repo"

    init_mod.uninstall(tmp_path, only="claude")
    assert json.loads((tmp_path / CLAUDE).read_text())["statusLine"] == theirs  # restored


def test_init_composes_with_a_user_level_statusline(tmp_path: Path, monkeypatch):
    # The real case: a user status line lives in ~/.claude/settings.json (user level), which a
    # repo-level line would shadow. Tycho records it (origin "user") and composes; we never
    # write the user file, so it resurfaces on its own when ours is removed (TYCHO-47).
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "other-statusline.ps1"}})
    )
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: home / f".{name}")

    _install(tmp_path)

    wrap = state.read_statusline_wrap(tmp_path)
    assert wrap == {"command": "other-statusline.ps1", "origin": "user"}
    init_mod.uninstall(tmp_path, only="claude")
    assert state.read_statusline_wrap(tmp_path) is None  # compose target forgotten


def test_init_keeps_the_users_own_statusline_keys(tmp_path: Path):
    # padding/refreshInterval are the user's to set; re-running init must not eat them.
    _install(tmp_path)
    settings = json.loads((tmp_path / CLAUDE).read_text())
    settings["statusLine"]["padding"] = 1
    (tmp_path / CLAUDE).write_text(json.dumps(settings))

    init_mod.init(tmp_path, only="claude", assume_yes=True)

    assert json.loads((tmp_path / CLAUDE).read_text())["statusLine"]["padding"] == 1


def test_uninstall_removes_our_statusline(tmp_path: Path):
    _install(tmp_path)
    init_mod.uninstall(tmp_path, only="claude")
    assert "statusLine" not in json.loads((tmp_path / CLAUDE).read_text())


def test_uninstall_leaves_someone_elses_statusline_alone(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    theirs = {"type": "command", "command": "~/.claude/other-statusline.sh"}
    (tmp_path / CLAUDE).write_text(json.dumps({"statusLine": theirs}))

    init_mod.uninstall(tmp_path, only="claude")

    assert json.loads((tmp_path / CLAUDE).read_text())["statusLine"] == theirs


# --- finding the repo from a subdirectory (TYCHO-79) -------------------------
#
# A shell prompt follows the user into subdirectories and supplies no payload, so `repo`
# arrives as the cwd rather than the root. Root-only resolution made that read as "not
# installed" — the badge silently blank for most of a session, indistinguishable from
# Tycho being absent, which is the exact silence Tycho exists to prevent.

def test_the_badge_survives_a_subdirectory(tmp_path: Path):
    _install(tmp_path)
    state.record_run(tmp_path, "claude", verdict="VERIFIED")
    deep = tmp_path / "tycho" / "checks"
    deep.mkdir(parents=True)

    assert status.line(deep) == "[TYCHO]"                     # was "" — blank below the root
    assert state.last_run(deep)["verdict"] == "VERIFIED"      # same state, not a fresh miss


def test_state_written_from_a_subdirectory_lands_at_the_root(tmp_path: Path):
    # One root per repo: a write from a subdir must not fork a second `.tycho/` under it.
    _install(tmp_path)
    deep = tmp_path / "src"
    deep.mkdir()

    state.record_run(deep, "claude", verdict="FAILED")

    assert not (deep / ".tycho").exists()
    assert state.last_run(tmp_path)["verdict"] == "FAILED"


def test_the_walk_stops_at_the_git_root(tmp_path: Path):
    # An unrelated parent's `.tycho/` is not ours to adopt: a sibling repo checked out
    # inside a directory that happens to have Tycho state must still read as "not installed".
    _install(tmp_path)
    other = tmp_path / "vendor" / "other-repo"
    other.mkdir(parents=True)
    (other / ".git").mkdir()

    assert status.line(other) == ""
    assert state.dir_for(other) == other / ".tycho"


def test_config_resolves_from_a_subdirectory(tmp_path: Path):
    # `.tycho.toml` is `.tycho/`'s sibling and must follow the same root, or `scope` run one
    # directory down reports "no scope set" and silently widens what the agent may edit.
    from tycho import config as config_mod

    config_mod.set_scope(tmp_path, ["src/**"])
    deep = tmp_path / "src" / "inner"
    deep.mkdir(parents=True)

    assert config_mod.load(deep).scope_include == ("src/**",)
    assert config_mod.path(deep) == tmp_path / ".tycho.toml"


def test_doctor_from_a_subdirectory_does_not_cry_wolf(tmp_path: Path):
    # TYCHO-79: state resolution and *harness config* resolution must agree. If only the
    # former walks, doctor finds the root's install record, looks for `.claude/settings.json`
    # beside itself one directory down, finds none, and reports the hook as ripped out —
    # a false alarm about wiring that is fine, which is its own kind of lie.
    from tycho import doctor

    _install(tmp_path)
    deep = tmp_path / "tycho"
    deep.mkdir()

    # `level` carries BROKEN, not `text` — asserting on the wrong field here passes vacuously.
    assert not any("BROKEN" in f.level for f in doctor.diagnose(deep))
    assert "installed" in doctor.liveness(deep)

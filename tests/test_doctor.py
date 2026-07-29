""" — the hook compatibility contract: does Tycho notice when it isn't running?

The failure guarded against here is the quiet one: hooks that look installed, a verdict
that never comes, and a developer who reads silence as "verified". So these tests care
less about pretty output than about two questions — does doctor catch a hook that would
not fire, and does it stay quiet when it has no evidence of a problem? A diagnostic that
cries wolf gets ignored exactly when it's finally right.
"""

import io
import json
import sys
import time
from pathlib import Path

import pytest

from tycho import cli
from tycho.wire import doctor
from tycho.wire import hook
from tycho.wire import install as init_mod
from tycho.store import state

CLAUDE = Path(".claude/settings.json")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No real $HOME: detection and discovery must not depend on this machine."""
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: empty / f".{name}")
    monkeypatch.setattr(init_mod.opencode_mod, "db_path", lambda: empty / "oc" / "opencode.db")
    monkeypatch.setattr(doctor.harness_mod, "home", lambda name: empty / f".{name}")


def _install(repo: Path) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    init_mod.init(repo, only="claude", assume_yes=True)


def _set_hook_command(repo: Path, command: str) -> None:
    """Rewrite the installed Stop hook command via JSON, not text replace.

    A path with spaces makes `hook_command()` return a quoted string whose quotes are
    backslash-escaped in the file — so a `.replace(hook_command(), ...)` on the raw text
    silently misses. Mutating the structure is correct regardless of path or quoting.
    """
    settings = repo / CLAUDE
    data = json.loads(settings.read_text())
    data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": command}]}]
    settings.write_text(json.dumps(data))


def _levels(findings) -> list[str]:
    return [f.level for f in findings]


# --- the state that makes any of this knowable -------------------------------

def test_init_stamps_the_schema_and_what_it_wired(tmp_path: Path):
    _install(tmp_path)
    assert state.installed_schema(tmp_path) == state.SCHEMA
    assert init_mod._is_tycho_hook(state.read_install(tmp_path)["claude"]["command"])


def test_uninstall_forgets_the_harness_so_doctor_stops_diagnosing_a_ghost(tmp_path: Path):
    _install(tmp_path)
    init_mod.uninstall(tmp_path, only="claude")
    assert state.read_install(tmp_path) == {}
    # `.tycho/` survives here only because this directory has no git of its own, so the
    # shadow history inside it is the project's ONLY record of what changed. `rmdir` refuses a
    # non-empty directory by design — an uninstall must not silently delete a history. In a
    # real git repo there is no shadow and the directory goes; `--purge` removes it either way.
    from tycho.store import shadow
    assert shadow.exists(tmp_path)
    assert not (state.dir_for(tmp_path) / "install.json").exists()  # nothing left to remember
    assert doctor.healthy(doctor.diagnose(tmp_path))  # gone is not broken


def test_uninstalling_one_harness_keeps_the_others_recorded(tmp_path: Path):
    # Auto-detect surfaces only Claude now, so install the second harness explicitly (its
    # installer is kept). Uninstalling Claude must leave the other's install record intact.
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    init_mod.init(tmp_path, only="cursor", assume_yes=True)
    init_mod.uninstall(tmp_path, only="claude")
    assert list(state.read_install(tmp_path)) == ["cursor"]


# --- the heartbeat: a dead hook can't write one ------------------------------

def test_the_hook_records_a_heartbeat_when_it_runs(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}}))
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(transcript)})

    assert hook.run(payload) is None  # nothing to verify — but the wiring still fired
    beat = state.last_run(tmp_path)
    assert beat["harness"] == "claude"
    assert beat["at"] == pytest.approx(time.time(), abs=10)


def test_prompt_submit_records_a_pending_beat(tmp_path: Path, monkeypatch):
    # the UserPromptSubmit hook marks a run in flight (pending), so the badge shows
    # frost-blue "verifying" for the whole turn — the Stop hook later clears it to the verdict.
    import io

    payload = json.dumps({"cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert hook.prompt_submit() == 0
    beat = state.last_run(tmp_path)
    assert beat["harness"] == "claude"
    assert beat["pending"] is True and beat.get("verdict") is None


def test_prompt_submit_never_raises_on_bad_stdin(tmp_path: Path, monkeypatch):
    # Same invariant as every hook: a UserPromptSubmit hook must never break the prompt.
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert hook.prompt_submit() == 0
    assert state.last_run(tmp_path) is None  # nothing written, nothing raised


def test_a_heartbeat_that_cannot_be_written_never_breaks_the_hook(tmp_path: Path, monkeypatch):
    # The invariant that outranks this entire feature: Tycho never breaks the agent's Stop.
    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state, "_write_json", boom)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}}))
    payload = json.dumps({"cwd": str(tmp_path), "transcript_path": str(transcript)})

    assert hook.run(payload) is None  # no exception escaped
    assert state.last_run(tmp_path) is None


def test_doctor_reports_the_heartbeat_age(tmp_path: Path):
    _install(tmp_path)
    state.record_run(tmp_path, "claude")
    assert "hook last fired just now (via claude)" in doctor.render(doctor.diagnose(tmp_path))


# --- what doctor must catch --------------------------------------------------

def test_a_command_that_does_not_resolve_is_broken(tmp_path: Path, monkeypatch):
    # The silent death: the entry is there, the harness runs it, nothing exists to run.
    monkeypatch.setattr(init_mod.spelling, "hook_command", lambda: "/nonexistent/python -m tycho.cli hook")
    _install(tmp_path)
    findings = doctor.diagnose(tmp_path)
    assert doctor.BROKEN in _levels(findings)
    assert not doctor.healthy(findings)
    broken = next(f for f in findings if f.level == doctor.BROKEN)
    assert "doesn't resolve" in broken.text and "tycho init" in broken.fix


def test_a_hook_that_vanished_from_config_is_broken(tmp_path: Path):
    # We recorded an install and the entry is gone: an upgrade, a hand-edit, a
    # teammate's settings landing on top. The user believes they're covered and isn't.
    _install(tmp_path)
    (tmp_path / CLAUDE).write_text(json.dumps({"model": "opus"}))
    findings = doctor.diagnose(tmp_path)
    assert doctor.BROKEN in _levels(findings)
    assert "gone from" in next(f for f in findings if f.level == doctor.BROKEN).text


def test_an_old_schema_is_outdated(tmp_path: Path):
    _install(tmp_path)
    path = state.dir_for(tmp_path) / "install.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "schema": state.SCHEMA - 1}))
    findings = doctor.diagnose(tmp_path)
    assert doctor.OUTDATED in _levels(findings)
    assert not doctor.healthy(findings)


def test_a_stale_path_to_a_deleted_venv_is_broken(tmp_path: Path):
    _install(tmp_path)
    _set_hook_command(tmp_path, "/gone/venv/bin/python -m tycho.cli hook")
    assert doctor.BROKEN in _levels(doctor.diagnose(tmp_path))


def test_the_console_script_form_is_not_flagged(tmp_path: Path):
    # `init.hook_command()` returns a console script or `<python> -m` depending on whether
    # the venv is on PATH right then, and comparing against it marked four working hooks
    # OUTDATED. Both forms run: anything that resolves and is ours passes.
    script = tmp_path / "venv" / "bin" / "tycho"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    _install(tmp_path)
    _set_hook_command(tmp_path, f"{script} hook")

    findings = doctor.diagnose(tmp_path)
    assert doctor.healthy(findings), "a resolvable tycho command must never read as broken"
    assert doctor.OUTDATED not in _levels(findings)


def test_malformed_config_is_broken_not_a_traceback(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / CLAUDE).write_text("{oops")
    assert doctor.BROKEN in _levels(doctor.diagnose(tmp_path))


# --- the turn record's exposure to git ---------------------------------------
# 0.1.0 shipped no gitignore step, so an upgraded repo can be committing turns.jsonl — the
# agent's prose and every command it ran. An ignore rule doesn't untrack what git follows.


def _repo(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return tmp_path


def _commit_tycho_dir(repo: Path) -> None:
    """What a 0.1.0-era `git add -A` did: .tycho/ committed, no ignore rule anywhere."""
    import subprocess

    state.dir_for(repo).mkdir(parents=True, exist_ok=True)
    (state.dir_for(repo) / "turns.jsonl").write_text('{"claims":["I hardcoded the creds"]}\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "work"], check=True)


def test_an_unignored_tycho_dir_is_reported(tmp_path: Path):
    repo = _repo(tmp_path)
    state.dir_for(repo).mkdir(parents=True)
    findings = doctor._exposure(repo)
    assert _levels(findings) == [doctor.EXPOSED]
    assert "tycho init" in findings[0].fix


def test_a_tracked_tycho_dir_is_reported_louder_and_init_is_not_the_fix(tmp_path: Path):
    # The one init cannot repair. Doctor names the command and refuses to run it: untracking
    # someone's files is their call, not a diagnostic's.
    repo = _repo(tmp_path)
    _commit_tycho_dir(repo)
    findings = doctor._exposure(repo)
    assert doctor.TRACKED in _levels(findings)
    tracked = next(f for f in findings if f.level == doctor.TRACKED)
    assert "git rm -r --cached .tycho/" in tracked.fix
    assert "cannot repair" in tracked.fix


def test_init_leaves_an_already_tracked_tycho_dir_tracked(tmp_path: Path):
    # The reason the finding is distinct: adding the ignore rule fixes only half of it.
    repo = _repo(tmp_path)
    _commit_tycho_dir(repo)
    _install(repo)
    assert doctor.TRACKED in _levels(doctor._exposure(repo))


def test_an_ignored_untracked_tycho_dir_says_nothing(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text(".tycho/\n")
    state.dir_for(repo).mkdir(parents=True)
    assert doctor._exposure(repo) == []


def test_exposure_is_silent_outside_a_git_repo(tmp_path: Path):
    # Fail safe: no git, no repo, no unreadable index may produce a privacy alarm.
    assert doctor._exposure(tmp_path) == []


def test_exposure_never_makes_doctor_unhealthy(tmp_path: Path):
    # It is a privacy finding, not a "Tycho isn't verifying" one — the footer must not lie.
    repo = _repo(tmp_path)
    _commit_tycho_dir(repo)
    _install(repo)
    findings = doctor.diagnose(repo)
    assert doctor.TRACKED in _levels(findings)
    assert doctor.healthy(findings)


def test_diagnose_reports_exposure(tmp_path: Path):
    repo = _repo(tmp_path)
    state.dir_for(repo).mkdir(parents=True)
    assert doctor.EXPOSED in _levels(doctor.diagnose(repo))


# --- the upgrade note: 0.2.0 went quiet --------------------------------------


def test_an_old_schema_install_is_told_that_0_2_0_is_quiet(tmp_path: Path):
    # "I updated and Tycho stopped working" is this release's likeliest ticket. Said once,
    # to the upgrader already running doctor — not per turn.
    _install(tmp_path)
    path = state.dir_for(tmp_path) / "install.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "schema": 1}))
    text = doctor.render(doctor.diagnose(tmp_path))
    assert "quiet by design" in text and "tycho show" in text


def test_a_current_install_is_not_told_anything_about_the_quiet(tmp_path: Path):
    _install(tmp_path)
    assert "quiet by design" not in doctor.render(doctor.diagnose(tmp_path))


# --- what doctor must NOT cry wolf about -------------------------------------

def test_a_fresh_install_that_has_not_fired_yet_is_not_broken(tmp_path: Path):
    # No heartbeat here only means no agent turn has finished. Calling that BROKEN
    # would train the user to ignore the word.
    _install(tmp_path)
    findings = doctor.diagnose(tmp_path)
    assert doctor.healthy(findings)
    assert doctor.BROKEN not in _levels(findings)
    assert "has not run here yet" in doctor.render(findings)


def test_a_repo_with_no_tycho_is_not_broken(tmp_path: Path):
    findings = doctor.diagnose(tmp_path)
    assert doctor.healthy(findings)
    assert "Tycho is not set up on this machine" in doctor.render(findings)


def test_an_uninstalled_harness_is_not_diagnosed(tmp_path: Path):
    _install(tmp_path)  # cursor was never installed; only claude should appear
    text = doctor.render(doctor.diagnose(tmp_path))
    assert "claude" in text and "cursor:" not in text


# --- harness version drift ----------------------------------------

def test_drift_reports_when_the_harness_moved_past_the_verified_version(monkeypatch):
    # Read the pin rather than restate it: these test the drift *rule*, and hardcoding the
    # version made a legitimate re-verification look like a regression.
    pinned = doctor.harness_mod.VERIFIED_AGAINST["claude"]["version"]
    monkeypatch.setattr(doctor, "_probe_version", lambda probe: "2.9.9 (Claude Code)")
    findings = doctor._harness_drift(["claude"])
    assert _levels(findings) == [doctor.DRIFT]
    assert f"verified against {pinned}" in findings[0].text and "2.9.9" in findings[0].text


def test_drift_is_silent_when_the_version_still_matches(monkeypatch):
    # The pinned version appearing anywhere in the --version line means "current".
    pinned = doctor.harness_mod.VERIFIED_AGAINST["claude"]["version"]
    monkeypatch.setattr(doctor, "_probe_version", lambda probe: f"{pinned} (Claude Code)")
    assert doctor._harness_drift(["claude"]) == []


def test_drift_is_silent_when_version_cannot_be_read(monkeypatch):
    # Missing binary / unparseable --version → can't tell → say nothing (fail open).
    monkeypatch.setattr(doctor, "_probe_version", lambda probe: None)
    assert doctor._harness_drift(["claude"]) == []


def test_drift_does_not_sink_healthy(monkeypatch):
    # A version bump is "re-verify", not "broken": it must never make doctor NOT healthy.
    monkeypatch.setattr(doctor, "_probe_version", lambda probe: "9.9.9")
    assert doctor.healthy(doctor._harness_drift(["claude"]))


def test_probe_version_never_raises_on_a_missing_binary():
    assert doctor._probe_version(("tycho-no-such-harness-binary", "--version")) is None


def test_every_enabled_harness_has_a_version_pin():
    # The pin lets `doctor` say "your harness moved past the contract we checked". Without
    # one it never warns, which looks exactly like "the contract is fine".
    for name in doctor.harness_mod.ENABLED_NAMES:
        pinned = doctor.harness_mod.VERIFIED_AGAINST.get(name)
        assert pinned, f"{name} is enabled but has no verified-against pin"
        assert pinned["version"] and pinned["probe"]


# --- resolution rules --------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_resolves_absolute_path_only_when_executable(tmp_path: Path):
    prog = tmp_path / "prog"
    prog.write_text("#!/bin/sh\n")
    assert doctor._resolves(f"{prog} hook") is False  # exists, but not +x
    prog.chmod(0o755)
    assert doctor._resolves(f"{prog} hook") is True
    assert doctor._resolves(f"{tmp_path / 'missing'} hook") is False


def test_resolves_an_existing_path_regardless_of_platform(tmp_path: Path):
    # An existing program path resolves, a missing one doesn't. On Windows existence is the
    # whole test; the POSIX sibling above adds the executable bit.
    prog = tmp_path / "prog"
    prog.write_text("#!/bin/sh\n")
    prog.chmod(0o755)
    assert doctor._resolves(f"{prog} hook") is True
    assert doctor._resolves(f"{tmp_path / 'missing'} hook") is False


def test_resolves_bare_name_via_path():
    assert doctor._resolves("sh hook") is True
    assert doctor._resolves("definitely-not-a-real-program-xyz hook") is False


def test_resolves_handles_unparseable_and_empty_commands():
    assert doctor._resolves('sh "unbalanced') is False
    assert doctor._resolves("") is False


# --- CLI ---------------------------------------------------------------------

def test_cli_doctor_exits_ok_when_healthy(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    assert cli.main(["doctor"]) == cli.ExitCode.OK
    assert "healthy" in capsys.readouterr().out


def test_cli_doctor_exits_unhealthy_when_broken(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_mod.spelling, "hook_command", lambda: "/nonexistent/python -m tycho.cli hook")
    _install(tmp_path)
    assert cli.main(["doctor"]) == cli.ExitCode.UNHEALTHY
    out = capsys.readouterr().out
    assert doctor.BROKEN in out and "NOT healthy" in out


def test_cli_doctor_survives_a_legacy_codepage_console(tmp_path: Path, monkeypatch):
    # A cp1252 console can't encode ✓/✗/•/→, and an unguarded print() then raises — a
    # traceback where the whole point is a fail-open verdict.
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252"))
    code = cli.main(["doctor"])  # must not raise
    sys.stdout.flush()
    assert code in (cli.ExitCode.OK, cli.ExitCode.UNHEALTHY)
    assert b"tycho doctor" in buf.getvalue()


def test_cli_doctor_never_edits_anything(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    settings = tmp_path / CLAUDE
    before = (settings.read_text(), settings.stat().st_mtime_ns)
    cli.main(["doctor"])
    assert (settings.read_text(), settings.stat().st_mtime_ns) == before


def test_verify_warns_loudly_when_the_hook_is_broken(tmp_path: Path, monkeypatch, capsys):
    # The diagnostic has to reach the command people actually run.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_mod.spelling, "hook_command", lambda: "/nonexistent/python -m tycho.cli hook")
    _install(tmp_path)
    cli.main(["verify"])
    err = capsys.readouterr().err
    assert doctor.BROKEN in err and "tycho init" in err


def test_verify_stays_quiet_when_the_hook_is_fine(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    cli.main(["verify"])
    assert doctor.BROKEN not in capsys.readouterr().err


def test_bare_tycho_defaults_to_verify(tmp_path: Path, monkeypatch, capsys):
    # `tycho` with no subcommand is the one-word on-demand verdict. No session
    # here, so it renders the same INDETERMINATE "no recent session" note `verify` does.
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == cli.ExitCode.OK
    assert "no recent session found" in capsys.readouterr().out


# --- `tycho help`: what it is, and whether it's on here -----------

def _boom(*a, **k):
    raise RuntimeError("disk gone")


def test_help_answers_is_it_on_here(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install(tmp_path)
    state.record_run(tmp_path, "claude")

    assert cli.main(["help"]) == cli.ExitCode.OK
    out = capsys.readouterr().out
    assert "Status here: installed (claude) — hook last fired just now" in out
    assert "Stop hook" in out  # what Tycho is
    for name, text in cli._COMMANDS.items():  # every command, one line each
        assert name in out and text in out


def test_help_tells_an_unhooked_repo_how_to_install(tmp_path: Path, monkeypatch, capsys):
    # Help is what a confused user reaches for; on a bare repo it must degrade, not crash.
    monkeypatch.chdir(tmp_path)

    assert cli.main(["help"]) == cli.ExitCode.OK
    assert "NOT set up — run `tycho install`" in capsys.readouterr().out


def test_help_reports_a_broken_hook_rather_than_claiming_it_is_live(tmp_path: Path, monkeypatch):
    # The whole point of the status line: it must not answer "installed" when the
    # installed thing could never fire.
    monkeypatch.setattr(init_mod.spelling, "hook_command", lambda: "/nonexistent/python -m tycho.cli hook")
    _install(tmp_path)

    assert "not working" in doctor.liveness(tmp_path)


def test_help_status_survives_a_doctor_that_blows_up(tmp_path: Path, monkeypatch, capsys):
    # A status line must never be the reason `tycho help` fails.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "_wired_harnesses", _boom)

    assert cli.main(["help"]) == cli.ExitCode.OK
    assert "status unknown (RuntimeError) — run `tycho doctor`" in capsys.readouterr().out


def test_the_claude_pin_has_real_transcript_data_behind_it():
    """A version pin is a claim that someone re-read the harness's output at that version, and
    a bare string cannot hold anyone to it — `2.1.210` sat here while the machine ran `2.1.220`
    for a whole release, warning on every `doctor` and meaning nothing.

    So the pin is tied to captured rows: bumping it requires a transcript the new version
    actually wrote, and the reader tests then run against that data. See the procedure beside
    `VERIFIED_AGAINST` for what re-verifying covers.
    """
    import json
    from pathlib import Path

    pinned = doctor.harness_mod.VERIFIED_AGAINST["claude"]["version"]
    fixture = Path(__file__).parent / "fixtures" / "transcript_attribution.jsonl"
    versions = {json.loads(ln).get("version")
                for ln in fixture.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert pinned in versions, (
        f"claude is pinned to {pinned} but {fixture.name} carries {sorted(v for v in versions if v)} "
        "— capture a transcript from the pinned version before moving the pin"
    )


def test_the_codex_pin_has_real_transcript_data_behind_it():
    """Same rule for Codex, and it is the harness that proved why the rule is needed: the pin
    sat at 0.144.4 across the release where Codex moved its shell tool to a shape the reader
    couldn't see, and a bare string held nobody to noticing.

    Codex spells its version `session_meta.payload.cli_version`, not a top-level `version`.
    """
    import json
    from pathlib import Path

    pinned = doctor.harness_mod.VERIFIED_AGAINST["codex"]["version"]
    fixture = Path(__file__).parent / "fixtures" / "codex_attribution.jsonl"
    versions = {(json.loads(ln).get("payload") or {}).get("cli_version")
                for ln in fixture.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert pinned in versions, (
        f"codex is pinned to {pinned} but {fixture.name} carries {sorted(v for v in versions if v)} "
        "— capture a transcript from the pinned version before moving the pin"
    )

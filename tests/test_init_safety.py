"""TYCHO-6 — `tycho init` against a real developer's config: detect, ask, never clobber.

The happy path lives in test_m4/test_m6. This file is the adversarial half: the states
a real machine is actually in — a hand-edited config with a trailing comma, a settings
file symlinked into a dotfiles repo, a read-only file, a write killed halfway. The bar
is that Tycho either does the right thing or does nothing, and says which.
"""

import json
import stat
import sys
from pathlib import Path

import pytest

from tycho import cli
from tycho import init as init_mod

CLAUDE = Path(".claude/settings.json")
CURSOR = Path(".cursor/hooks.json")

# The POSIX permission model (an executable/mode bit, dir perms that block a child
# write) has no faithful Windows equivalent — chmod there only toggles read-only. These
# guard the tests that assert on it; the behaviour they cover is real, just POSIX-only.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode semantics")


def _no(name: str) -> bool:
    return False


@pytest.fixture(autouse=True)
def _isolate_detection(tmp_path, monkeypatch):
    """Point every harness home at an empty dir — detection must not read the dev's real $HOME."""
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: empty / f".{name}")
    monkeypatch.setattr(init_mod.opencode_mod, "db_path", lambda: empty / "oc" / "opencode.db")


# --- detection ---------------------------------------------------------------

def test_detect_finds_nothing_in_a_bare_repo(tmp_path: Path):
    assert init_mod.detect(tmp_path) == []


def test_detect_ignores_a_disabled_harness(tmp_path: Path):
    # Cursor ran in this repo (its dotdir exists), but only Claude is enabled in usage now,
    # so detection must not surface it — init won't prompt to wire it up.
    (tmp_path / ".cursor").mkdir()
    assert init_mod.detect(tmp_path) == []


def test_detect_sees_a_harness_installed_for_this_user(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: home / f".{name}")
    assert init_mod.detect(tmp_path) == ["claude"]


def test_detect_skips_opencode_even_when_present(tmp_path: Path, monkeypatch):
    db = tmp_path / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setattr(init_mod.opencode_mod, "db_path", lambda: db)
    # The detector still sees OpenCode's XDG data dir (kept code)…
    assert init_mod._is_present("opencode", tmp_path) is True
    # …but usage-facing detection is gated to Claude, so it isn't surfaced.
    assert init_mod.detect(tmp_path) == []


def test_init_installs_only_detected_harnesses(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, assume_yes=True)
    assert (tmp_path / CLAUDE).exists()
    assert not (tmp_path / CURSOR).exists()  # cursor isn't here — don't invent it
    assert not (tmp_path / ".opencode").exists()


def test_init_on_a_repo_with_no_harness_says_so_and_writes_nothing(tmp_path: Path):
    lines = init_mod.init(tmp_path, assume_yes=True)
    assert "no supported harness detected" in lines[0]
    assert list(tmp_path.iterdir()) == [tmp_path / "elsewhere"]  # only the fixture's dir


# --- consent -----------------------------------------------------------------

def test_init_asks_before_each_detected_harness(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()  # present but disabled — must not be offered
    asked = []
    init_mod.init(tmp_path, confirm=lambda name: asked.append(name) or True)
    assert asked == ["claude"]  # only enabled harnesses are offered, one prompt each


def test_declining_a_harness_leaves_it_untouched(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    lines = init_mod.init(tmp_path, confirm=_no)
    assert lines == ["claude: skipped"]
    assert not (tmp_path / CLAUDE).exists()


def test_yes_installs_without_asking(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, assume_yes=True, confirm=lambda name: pytest.fail("must not prompt"))
    assert (tmp_path / CLAUDE).exists()


def test_prompt_defaults_to_yes_on_a_bare_enter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert init_mod._ask("claude") is True


def test_prompt_takes_no_for_an_answer(monkeypatch):
    for reply in ("n", "N", "no", " No "):
        monkeypatch.setattr("builtins.input", lambda prompt, r=reply: r)
        assert init_mod._ask("claude") is False


def test_non_interactive_run_without_yes_installs_nothing(tmp_path: Path, monkeypatch):
    # CI has no tty: prompting is impossible, so installing unasked would be the very
    # thing this ticket exists to stop. Say how to proceed instead.
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    lines = init_mod.init(tmp_path)
    assert "--yes" in lines[0]
    assert not (tmp_path / CLAUDE).exists()


def test_harness_flag_installs_even_when_not_detected(tmp_path: Path):
    lines = init_mod.init(tmp_path, only="codex", assume_yes=True)
    assert (tmp_path / ".codex" / "hooks.json").exists()
    assert not (tmp_path / CLAUDE).exists()
    # The codex install line, plus the seeded .tycho.toml line (TYCHO-55).
    assert len(lines) == 2 and lines[0].startswith("codex")
    assert (tmp_path / ".tycho.toml").exists()


# --- command builder: forward slashes for Git Bash (TYCHO-43) ----------------

def test_command_for_forward_slashes_windows_paths(monkeypatch):
    # Claude Code runs hook/statusLine through Git Bash, which eats unquoted backslashes
    # and the command silently dies. _command_for must emit forward slashes on any host.
    monkeypatch.setattr(init_mod.shutil, "which", lambda name: None)  # force interpreter branch
    monkeypatch.setattr(init_mod.sys, "executable", r"C:\Users\me\Swail Labs\.venv\Scripts\python.exe")
    cmd = init_mod.hook_command()
    assert "\\" not in cmd
    assert cmd == '"C:/Users/me/Swail Labs/.venv/Scripts/python.exe" -m tycho.cli hook'

    # console-script branch (tycho.exe on PATH) is forward-slashed too
    monkeypatch.setattr(init_mod.shutil, "which", lambda name: r"C:\Tools\tycho.exe")
    assert init_mod.status_command() == "C:/Tools/tycho.exe statusline"


# --- SessionStart hook (TYCHO-53) --------------------------------------------

def test_install_wires_the_sessionstart_hook(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)
    data = json.loads((tmp_path / CLAUDE).read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert any(init_mod._is_tycho_session_start(c) for c in cmds)
    assert any("SessionStart" in ln for ln in lines)


def test_uninstall_removes_the_sessionstart_hook(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    lines = init_mod.uninstall(tmp_path, only="claude")
    data = json.loads((tmp_path / CLAUDE).read_text())
    assert "SessionStart" not in (data.get("hooks") or {})  # gone, and no empty container
    assert any("SessionStart" in ln for ln in lines)


def test_codex_install_and_uninstall_round_trip_the_sessionstart_hook(tmp_path: Path):
    # Codex gets the bootup-notice hook too (TYCHO-72), stripped alongside Stop in one write.
    codex = tmp_path / ".codex" / "hooks.json"
    codex.parent.mkdir()
    init_mod.init(tmp_path, only="codex", assume_yes=True)
    installed = json.loads(codex.read_text())
    ss_cmds = [h["command"] for g in installed["hooks"]["SessionStart"] for h in g["hooks"]]
    assert any(init_mod._is_tycho_session_start(c) for c in ss_cmds)
    lines = init_mod.uninstall(tmp_path, only="codex")
    hooks = json.loads(codex.read_text()).get("hooks") or {}
    assert "SessionStart" not in hooks and "Stop" not in hooks  # both gone, no empty containers
    assert any("SessionStart" in ln for ln in lines)


# --- UserPromptSubmit hook: mid-run frost-blue badge (TYCHO-94) ---------------

def test_install_wires_the_userpromptsubmit_hook(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)
    data = json.loads((tmp_path / CLAUDE).read_text())
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert any(init_mod._is_tycho_prompt_submit(c) for c in cmds)
    assert any("UserPromptSubmit" in ln for ln in lines)


def test_uninstall_removes_the_userpromptsubmit_hook(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    lines = init_mod.uninstall(tmp_path, only="claude")
    data = json.loads((tmp_path / CLAUDE).read_text())
    assert "UserPromptSubmit" not in (data.get("hooks") or {})  # gone, no empty container
    assert any("UserPromptSubmit" in ln for ln in lines)


def test_install_is_idempotent_for_the_userpromptsubmit_hook(tmp_path: Path):
    # a second init must not stack a duplicate UserPromptSubmit group (mirrors Stop/SessionStart)
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    data = json.loads((tmp_path / CLAUDE).read_text())
    ours = [
        h for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]
        if init_mod._is_tycho_prompt_submit(h["command"])
    ]
    assert len(ours) == 1


# --- first-run offer (TYCHO-49) ----------------------------------------------

def test_first_run_offers_and_installs_on_yes(tmp_path: Path):
    (tmp_path / ".claude").mkdir()  # a supported agent is here, but Tycho isn't wired
    lines = init_mod.offer_first_run(tmp_path, confirm=lambda h: True)
    assert (tmp_path / CLAUDE).exists()  # said yes → wired
    assert any("claude" in ln for ln in lines)


def test_first_run_decline_writes_nothing_and_prints_the_one_liner(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    lines = init_mod.offer_first_run(tmp_path, confirm=lambda h: False)
    assert not (tmp_path / CLAUDE).exists()  # declined → no config written
    assert any("tycho init" in ln for ln in lines)


def test_first_run_non_interactive_prints_one_liner_without_prompting(tmp_path: Path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt without a TTY"))
    lines = init_mod.offer_first_run(tmp_path)  # confirm=None, no TTY
    assert not (tmp_path / CLAUDE).exists()
    assert any("tycho init" in ln for ln in lines)


def test_first_run_is_offered_only_once_per_repo(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    assert init_mod.offer_first_run(tmp_path, confirm=lambda h: False)  # offered
    assert init_mod.offer_first_run(tmp_path, confirm=lambda h: False) == []  # never again


def test_first_run_silent_when_already_wired(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)  # already set up
    assert init_mod.offer_first_run(tmp_path, confirm=lambda h: pytest.fail("must not ask")) == []


def test_first_run_silent_in_a_repo_with_no_agent(tmp_path: Path):
    assert init_mod.offer_first_run(tmp_path, confirm=lambda h: pytest.fail("must not ask")) == []


# --- malformed config: refuse, don't "recover" -------------------------------

def test_init_refuses_malformed_json_and_preserves_it_byte_for_byte(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    original = '{"model": "opus",}\n'  # trailing comma — a hand edit, not garbage
    settings.write_text(original)
    lines = init_mod.init(tmp_path, assume_yes=True)
    assert init_mod.REFUSED in lines[0] and "not valid JSON" in lines[0]
    assert settings.read_text() == original  # the whole point: still there


def test_init_refuses_json_that_is_not_an_object(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]\n")
    lines = init_mod.init(tmp_path, assume_yes=True)
    assert init_mod.REFUSED in lines[0]
    assert settings.read_text() == "[1, 2, 3]\n"


def test_uninstall_purge_removes_repo_local_state_and_config(tmp_path: Path):
    from tycho import config as config_mod
    from tycho import state

    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)  # drops .tycho.toml
    state.dir_for(tmp_path).mkdir(exist_ok=True)
    (state.dir_for(tmp_path) / "catches.json").write_text("{}")  # a catch trail to purge
    assert config_mod.path(tmp_path).exists() and state.dir_for(tmp_path).exists()

    # default uninstall leaves both; --purge deletes both; a second --purge is a no-op
    init_mod.uninstall(tmp_path, only="claude")
    assert config_mod.path(tmp_path).exists() and state.dir_for(tmp_path).exists()
    lines = init_mod.uninstall(tmp_path, only="claude", purge=True)
    assert not config_mod.path(tmp_path).exists() and not state.dir_for(tmp_path).exists()
    assert any("removed .tycho/" in ln for ln in lines)
    assert any("removed .tycho.toml" in ln for ln in lines)
    again = init_mod.uninstall(tmp_path, only="claude", purge=True)
    assert all(init_mod.REFUSED not in ln for ln in again)  # idempotent, not an error


def test_uninstall_refuses_malformed_json_too(tmp_path: Path):
    # Same rule both ways: removing our hook isn't worth risking the rest of the file.
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text("{oops\n")
    line = init_mod.uninstall(tmp_path, only="claude")[0]
    assert init_mod.REFUSED in line
    assert settings.read_text() == "{oops\n"


def test_an_empty_config_file_is_not_malformed(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text("")  # harnesses do leave these around
    init_mod.init(tmp_path, assume_yes=True)
    data = json.loads(settings.read_text())
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][0]["hooks"][0]["command"])


def test_init_refuses_a_handwritten_opencode_plugin(tmp_path: Path):
    plugin = tmp_path / ".opencode" / "plugins" / "tycho.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// mine\n")
    lines = init_mod.init(tmp_path, only="opencode", assume_yes=True)
    assert init_mod.REFUSED in lines[0]
    assert plugin.read_text() == "// mine\n"


# --- backups, permissions, atomicity -----------------------------------------

def test_existing_config_is_backed_up_before_the_first_mutation(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus"}))
    init_mod.init(tmp_path, assume_yes=True)
    backup = settings.with_name(settings.name + ".tycho.bak")
    assert json.loads(backup.read_text()) == {"model": "opus"}  # the pre-Tycho state


def test_no_backup_and_no_write_when_already_current(tmp_path: Path):
    # A repeat run must not churn mtimes or overwrite the backup of the real original.
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, assume_yes=True)
    settings = tmp_path / CLAUDE
    before = settings.stat().st_mtime_ns
    lines = init_mod.init(tmp_path, assume_yes=True)
    assert "already current" in lines[0]
    assert settings.stat().st_mtime_ns == before
    assert not settings.with_name(settings.name + ".tycho.bak").exists()


@posix_only
def test_file_permissions_are_preserved(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    settings.chmod(0o640)  # a deliberately private config
    init_mod.init(tmp_path, assume_yes=True)
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640


def test_read_only_config_is_refused_not_replaced(tmp_path: Path):
    # A rename lands regardless of the *file's* mode, so without an explicit check a
    # read-only settings file would be silently swapped out.
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "opus"}')
    settings.chmod(0o444)
    lines = init_mod.init(tmp_path, assume_yes=True)
    assert init_mod.REFUSED in lines[0] and "read-only" in lines[0]
    assert json.loads(settings.read_text()) == {"model": "opus"}


def test_symlinked_config_is_written_through_not_replaced(tmp_path: Path):
    # The dotfiles-repo case: `.claude/settings.json -> ~/dotfiles/claude.json`.
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"model": "opus"}))
    link = tmp_path / CLAUDE
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    init_mod.init(tmp_path, assume_yes=True)

    assert link.is_symlink(), "the symlink must survive — it's the user's dotfiles wiring"
    data = json.loads(real.read_text())  # the edit landed in the dotfiles repo
    assert data["model"] == "opus"
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][0]["hooks"][0]["command"])


def test_missing_parent_dirs_are_created(tmp_path: Path):
    init_mod.init(tmp_path, only="opencode", assume_yes=True)
    assert (tmp_path / ".opencode" / "plugins" / "tycho.js").is_file()  # two levels deep


def test_an_interrupted_write_leaves_the_original_intact(tmp_path: Path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / CLAUDE
    original = json.dumps({"model": "opus"})
    settings.write_text(original)

    def die(self, target):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "replace", die)  # kill the run at the rename
    lines = init_mod.init(tmp_path, assume_yes=True)

    assert init_mod.REFUSED in lines[0], "a dead write is a refusal, not a traceback"
    assert settings.read_text() == original  # never truncated
    assert not settings.with_name(settings.name + ".tycho-tmp").exists()  # no litter


@posix_only
def test_an_unwritable_parent_dir_is_refused_not_a_traceback(tmp_path: Path):
    # The file's own write bit says yes, the directory says no — so the read-only check
    # passes and the failure lands mid-write. It still has to come back as a status line.
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / CLAUDE
    settings.write_text('{"model": "opus"}')
    (tmp_path / ".claude").chmod(0o555)
    try:
        lines = init_mod.init(tmp_path, assume_yes=True)
        assert init_mod.REFUSED in lines[0] and "cannot write" in lines[0]
        assert json.loads(settings.read_text()) == {"model": "opus"}
    finally:
        (tmp_path / ".claude").chmod(0o755)  # let tmp_path cleanup run


def test_config_path_containing_spaces_round_trips(tmp_path: Path):
    repo = tmp_path / "my repo (v2)"
    (repo / ".claude").mkdir(parents=True)
    init_mod.init(repo, assume_yes=True)
    data = json.loads((repo / CLAUDE).read_text())
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][0]["hooks"][0]["command"])


# --- cross-platform hook recognition (TYCHO-41) ------------------------------

def test_is_tycho_hook_recognizes_every_platform_form():
    ok = [
        "/home/me/.venv/bin/tycho hook",                 # POSIX console script
        r"C:\Users\me\.venv\Scripts\tycho.EXE hook",     # Windows console script
        r"C:\Users\me\.venv\Scripts\tycho.exe hook",     # lower-case too
        '"C:\\Program Files\\t\\tycho.exe" hook',        # quoted (path with a space)
        "/usr/bin/python -m tycho.cli hook",             # module form
        r'"C:\Py\python.exe" -m tycho.cli hook',         # quoted module form
    ]
    for command in ok:
        assert init_mod._is_tycho_hook(command), command
    for command in ["make lint", "/bin/tychonaut hook", "tycho verify", 42, None]:
        assert not init_mod._is_tycho_hook(command), command


def test_hook_argv_keeps_windows_paths_and_strips_quotes():
    # The opencode plugin embeds this argv verbatim; a POSIX split would eat the
    # backslashes of a quoted Windows path and hand Bun a mangled program.
    assert init_mod.hook_argv(r'"C:\Swail Labs\.venv\Scripts\python.exe" -m tycho.cli hook') == [
        r"C:\Swail Labs\.venv\Scripts\python.exe", "-m", "tycho.cli", "hook",
    ]
    assert init_mod.hook_argv("/opt/venv/bin/tycho hook") == ["/opt/venv/bin/tycho", "hook"]
    assert init_mod.hook_argv('sh "unbalanced') == []  # nothing runnable


def test_quote_program_wraps_only_when_needed():
    assert init_mod._quote_program(r"C:\a\b\tycho.exe") == r"C:\a\b\tycho.exe"
    assert init_mod._quote_program(r"C:\Swail Labs\tycho.exe") == r'"C:\Swail Labs\tycho.exe"'
    assert init_mod._quote_program('"C:\\already quoted\\x"') == '"C:\\already quoted\\x"'


def test_reinit_over_a_windows_exe_entry_does_not_duplicate(tmp_path: Path):
    # The bug: an existing `tycho.EXE hook` entry went unrecognized, so re-init appended
    # a second group instead of replacing it. With recognition fixed, exactly one remains.
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": r"C:\v\Scripts\tycho.EXE hook"}]},
    ]}}))
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    groups = json.loads(settings.read_text())["hooks"]["Stop"]
    tycho_hooks = [h for g in groups for h in g.get("hooks", [])
                   if init_mod._is_tycho_hook(h.get("command"))]
    assert len(tycho_hooks) == 1


# --- unrelated content survives ----------------------------------------------

def test_unusual_but_valid_hook_structures_survive(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "make lint"}]},
                {"hooks": []},  # an empty group the user left behind
            ],
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }))
    init_mod.init(tmp_path, assume_yes=True)
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["hooks"]["Stop"][0] == {
        "matcher": "*", "hooks": [{"type": "command", "command": "make lint"}]
    }
    assert data["hooks"]["Stop"][1] == {"hooks": []}
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][-1]["hooks"][0]["command"])


# --- CLI ---------------------------------------------------------------------

def test_cli_init_yes_exits_ok(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    assert cli.main(["init", "--yes"]) == cli.ExitCode.OK
    assert "installed" in capsys.readouterr().out


def test_cli_init_exits_nonzero_when_it_refuses(tmp_path: Path, monkeypatch, capsys):
    # A provisioning script must fail loudly rather than leave the repo silently unhooked.
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text("{nope")
    assert cli.main(["init", "--yes"]) == cli.ExitCode.INTERNAL
    assert init_mod.REFUSED in capsys.readouterr().out


def test_cli_init_harness_flag_scopes_the_install(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--harness", "claude", "--yes"]) == cli.ExitCode.OK
    assert (tmp_path / CLAUDE).exists()


def test_cli_init_rejects_a_disabled_harness(tmp_path: Path, monkeypatch):
    # Cursor's installer still exists, but the CLI no longer accepts it — only Claude is
    # exposed in usage. argparse rejects the disabled choice the same as an unknown one.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--harness", "cursor"])
    assert exc.value.code == cli.ExitCode.USAGE


def test_cli_init_rejects_an_unknown_harness(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--harness", "emacs"])
    assert exc.value.code == cli.ExitCode.USAGE

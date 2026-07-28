"""— `tycho init` against a real developer's config: detect, ask, never clobber.

The happy path lives in test_hooks/test_uninstall. This file is the adversarial half: the states
a real machine is actually in — a hand-edited config with a trailing comma, a settings
file symlinked into a dotfiles repo, a read-only file, a write killed halfway. The bar
is that Tycho either does the right thing or does nothing, and says which.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tycho import cli
from tycho import install as init_mod

CLAUDE = Path(".claude/settings.json")
CURSOR = Path(".cursor/hooks.json")
HOOK = "prepare-commit-msg"

# chmod on Windows only toggles read-only, so mode-bit assertions have no faithful
# equivalent there. The behaviour these cover is real, just POSIX-only.
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
    # The codex install line, plus the seeded .tycho.toml line.
    assert len(lines) == 2 and lines[0].startswith("codex")
    assert (tmp_path / ".tycho.toml").exists()


# --- command builder: forward slashes for Git Bash ----------------

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


# --- SessionStart hook --------------------------------------------

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
    # Codex gets the bootup-notice hook too, stripped alongside Stop in one write.
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


# --- UserPromptSubmit hook: mid-run frost-blue badge ---------------

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


# --- first-run offer ----------------------------------------------

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


# --- cross-platform hook recognition ------------------------------

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


# --- the commit-trailer git hook (strategy §6.6) ------------------------------
#
# The highest-risk file this module writes: not config, but code that runs inside every
# `git commit`. Same bar as everywhere else — merge, back up, refuse what we can't parse —
# plus one only a hook has: never fail the commit.


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    (repo / ".claude").mkdir()
    return repo


def _hook_path(repo: Path) -> Path:
    return init_mod.git_hooks_dir(repo) / HOOK


def test_no_git_hook_outside_a_git_repo(tmp_path: Path):
    # A plain directory is not a repo. Say nothing and write nothing, rather than inventing
    # a `.git/` for a `git init` the user never ran.
    (tmp_path / ".claude").mkdir()
    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)
    assert init_mod.git_hooks_dir(tmp_path) is None
    assert not any(ln.startswith("git:") for ln in lines)


def test_init_installs_the_commit_trailer_hook(tmp_path: Path):
    repo = _init_repo(tmp_path)
    lines = init_mod.init(repo, only="claude", assume_yes=True)
    hook = _hook_path(repo)
    assert hook.is_file()
    assert init_mod.attest_command() in hook.read_text()
    assert any(HOOK in ln for ln in lines)


def test_the_hook_block_can_neither_exit_nor_fail(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    block = _hook_path(repo).read_text()
    assert "|| :" in block, "a non-zero exit must not propagate, even under `set -e`"
    assert ">/dev/null 2>&1" in block, "output must not leak into the commit flow"
    body = block.split(init_mod._GIT_BEGIN)[1].split(init_mod._GIT_END)[0]
    assert "exit" not in body, "our block must never exit — a foreign hook's lines still run"


def test_installing_the_hook_twice_does_not_stack_it(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    first = _hook_path(repo).read_text()
    init_mod.init(repo, only="claude", assume_yes=True)
    text = _hook_path(repo).read_text()
    assert text == first
    assert text.count(init_mod._GIT_BEGIN) == 1


def test_a_stale_hook_block_is_refreshed_in_place(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(init_mod.spelling, "attest_command", lambda: "/old/python -m tycho.attest")
    init_mod.init(repo, only="claude", assume_yes=True)
    monkeypatch.setattr(init_mod.spelling, "attest_command", lambda: "/new/python -m tycho.attest")
    init_mod.init(repo, only="claude", assume_yes=True)
    text = _hook_path(repo).read_text()
    assert "/new/python" in text and "/old/python" not in text
    assert text.count(init_mod._GIT_BEGIN) == 1


def test_an_existing_hook_keeps_every_line_it_had(tmp_path: Path):
    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/bash\nset -euo pipefail\necho theirs\nexit 0\n"
    hook.write_text(original)
    hook.chmod(0o755)

    init_mod.init(repo, only="claude", assume_yes=True)

    text = hook.read_text()
    for line in original.splitlines():
        assert line in text
    # Ours goes right after the shebang: their `exit 0` would otherwise skip us entirely.
    assert text.splitlines()[0] == "#!/bin/bash"
    assert text.index(init_mod._GIT_BEGIN) < text.index("set -euo pipefail")
    assert hook.with_name(hook.name + ".tycho.bak").read_text() == original


def test_a_hook_we_cannot_parse_is_refused_not_clobbered(tmp_path: Path):
    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
    hook.write_text(original)

    lines = init_mod.init(repo, only="claude", assume_yes=True)

    assert hook.read_text() == original, "the whole point: still there, byte for byte"
    assert any(init_mod.REFUSED in ln and ln.startswith("git") for ln in lines)
    # …and the refusal doesn't take the harness install down with it.
    assert (repo / CLAUDE).exists()


def test_a_hook_with_no_shebang_is_refused(tmp_path: Path):
    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("echo hi\n")
    lines = init_mod.init(repo, only="claude", assume_yes=True)
    assert hook.read_text() == "echo hi\n"
    assert any(init_mod.REFUSED in ln for ln in lines)


def test_uninstall_deletes_a_hook_that_was_only_ours(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert _hook_path(repo).exists()

    lines = init_mod.uninstall(repo, only="claude")

    assert not _hook_path(repo).exists()
    assert not _hook_path(repo).with_name(HOOK + ".tycho.bak").exists()  # no litter either
    assert any(HOOK in ln for ln in lines)


def test_uninstall_restores_a_foreign_hook_byte_for_byte(tmp_path: Path):
    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/sh\n# my hook\necho theirs\n"
    hook.write_text(original)

    init_mod.init(repo, only="claude", assume_yes=True)
    assert hook.read_text() != original
    init_mod.uninstall(repo, only="claude")

    assert hook.read_text() == original


def test_uninstall_is_silent_and_idempotent_when_the_hook_was_never_installed(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert not any("git" in ln for ln in init_mod.uninstall(repo, only="claude"))
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.uninstall(repo, only="claude")
    assert not any("git" in ln for ln in init_mod.uninstall(repo, only="claude"))


def test_the_hook_lands_where_core_hookspath_points(tmp_path: Path):
    # A repo that moved its hooks (husky, lefthook) would otherwise get a hook git never
    # runs — the silently-dead-hook failure, installed on purpose.
    repo = _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "myhooks"],
                   check=True, capture_output=True)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert (repo / "myhooks" / HOOK).is_file()
    assert not (repo / ".git" / "hooks" / HOOK).exists()


# --- the machine-wide install (strategy §6.7) --------------------------------
#
# A user-level hook fires in every repo on the machine. These pin the three things that make
# that safe: consent, a run-time guard, and an uninstall that restores the file exactly.


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch) -> Path:
    """A fake `~/.claude` that exists. Nothing here may touch the developer's real one."""
    home = tmp_path / "userhome" / ".claude"
    home.mkdir(parents=True)
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: home.parent / f".{name}")
    return home


def test_global_install_is_never_implied_by_a_plain_init(tmp_path: Path, user_home: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    assert not (user_home / "settings.json").exists()
    assert init_mod.global_installed() is False


def test_global_install_asks_first_and_writes_nothing_on_no(user_home: Path):
    lines = init_mod.init_global(confirm=lambda: False)
    assert not (user_home / "settings.json").exists()
    assert "skipped" in lines[0]
    assert init_mod.global_installed() is False


def test_global_install_wires_the_user_level_config_on_yes(user_home: Path):
    lines = init_mod.init_global(confirm=lambda: True)

    data = json.loads((user_home / "settings.json").read_text())
    command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert init_mod._is_tycho_hook(command), "uninstall must recognize what install wrote"
    assert init_mod.global_installed() is True
    assert any("uninstall --global" in ln for ln in lines)  # loud about how to undo it


def test_the_global_command_refuses_to_fire_outside_a_git_repo(user_home: Path):
    init_mod.init_global(confirm=lambda: True)
    command = json.loads((user_home / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    # Guard 1: no repo, no run — otherwise a user-level hook sprinkles `.tycho/` across
    # every scratch directory the agent is ever launched from.
    assert command.startswith("git rev-parse --show-toplevel >/dev/null 2>&1 || exit 0;")
    # Guard 2: a repo with its own install owns it. This is the anti-double-fire rule, and
    # it holds for repos wired long before `--global` was ever run.
    assert "grep -qsE" in command and ".claude/settings.json" in command


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX /bin/sh to run the guard")
def test_the_global_guard_actually_behaves_that_way(tmp_path: Path, user_home: Path):
    """Assert on the *behaviour* of the guard, not just its text — it is a shell fragment,
    and a shell fragment that looks right and exits 1 is a broken install."""
    init_mod.init_global(confirm=lambda: True)
    command = json.loads((user_home / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    guard = command.split("; ")[0] + "; " + command.split("; ")[1] + "; echo FIRED"

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    out = subprocess.run(["/bin/sh", "-c", guard], cwd=plain, capture_output=True, text=True)
    assert out.returncode == 0 and "FIRED" not in out.stdout  # quiet, and not an error

    repo = _init_repo(tmp_path)
    out = subprocess.run(["/bin/sh", "-c", guard], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0 and "FIRED" in out.stdout  # a bare repo: global runs

    init_mod.init(repo, only="claude", assume_yes=True)  # …until the repo has its own
    out = subprocess.run(["/bin/sh", "-c", guard], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0 and "FIRED" not in out.stdout, "double fire"


def test_a_plain_init_defers_to_an_active_global_install(tmp_path: Path, user_home: Path):
    init_mod.init_global(confirm=lambda: True)
    repo = _init_repo(tmp_path)

    lines = init_mod.init(repo, assume_yes=True)

    assert not (repo / CLAUDE).exists(), "two Stop hooks would fire on every turn"
    assert any("global install is active" in ln for ln in lines)
    # …but the commit trailer is per-repo and global can't provide it, so it still lands.
    assert _hook_path(repo).is_file()


def test_an_explicit_harness_flag_still_installs_per_repo_under_a_global_install(
    tmp_path: Path, user_home: Path
):
    init_mod.init_global(confirm=lambda: True)
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert (repo / CLAUDE).exists()  # the user named it; not ours to second-guess


def test_global_install_leaves_an_existing_user_statusline_alone(user_home: Path):
    settings = user_home / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mybadge"}}))

    lines = init_mod.init_global(confirm=lambda: True)

    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "mybadge"  # untouched, not composed with
    assert any("left your user-level statusLine alone" in ln for ln in lines)


def test_global_install_takes_an_empty_statusline_slot(user_home: Path):
    init_mod.init_global(confirm=lambda: True)
    data = json.loads((user_home / "settings.json").read_text())
    assert init_mod._is_tycho_status(data["statusLine"]["command"])


def test_global_uninstall_restores_the_user_config_byte_for_byte(user_home: Path):
    settings = user_home / "settings.json"
    original = json.dumps({"model": "opus", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "make lint"}]},
    ]}}, indent=2) + "\n"
    settings.write_text(original)

    init_mod.init_global(confirm=lambda: True)
    assert init_mod.global_installed() is True
    init_mod.uninstall_global()

    assert settings.read_text() == original
    assert init_mod.global_installed() is False
    assert not list(init_mod._commands_dir(Path.cwd(), init_mod.GLOBAL).glob("tycho*.md"))
    assert init_mod.uninstall_global()  # idempotent, and says something rather than raising


def test_global_install_refuses_a_user_config_it_cannot_parse(user_home: Path):
    settings = user_home / "settings.json"
    settings.write_text('{"model": "opus",}\n')  # a hand edit, not garbage

    lines = init_mod.init_global(confirm=lambda: True)

    assert init_mod.REFUSED in lines[0]
    assert settings.read_text() == '{"model": "opus",}\n'


def test_global_install_says_so_when_the_harness_is_not_installed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: tmp_path / "nowhere")
    lines = init_mod.init_global(assume_yes=True)
    assert "isn't installed" in lines[0]
    assert not (tmp_path / "nowhere").exists()


def test_global_install_needs_a_terminal_or_an_explicit_yes(user_home: Path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    lines = init_mod.init_global()
    assert "--yes" in lines[0]
    assert not (user_home / "settings.json").exists()


def test_the_global_prompt_defaults_to_no(user_home: Path, monkeypatch, capsys):
    # `[y/N]`, not the per-repo `[Y/n]`: a bare Enter must never install machine-wide.
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert init_mod._ask_global() is False
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert init_mod._ask_global() is True
    assert str(user_home / "settings.json") in capsys.readouterr().out  # names what it writes


def test_a_guarded_global_command_is_recognized_on_every_platform_form():
    """Uninstall removes exactly what install wrote — including through the guard prefix,
    and including Windows' `tycho.exe` console script."""
    for program in ("/home/me/.venv/bin/tycho", r"C:\Users\me\.venv\Scripts\tycho.EXE",
                    '"C:/Program Files/t/tycho.exe"', "/usr/bin/python -m tycho.cli"):
        command = init_mod._GLOBAL_GUARD + f"{program} hook"
        assert init_mod._is_tycho_hook(command), command
        assert init_mod._is_tycho_owned(command), command
    assert not init_mod._is_tycho_hook(init_mod._GLOBAL_GUARD + "/bin/tychonaut hook")


# --- .gitignore for `.tycho/` -----------------------------------
#
# Machine-local, and `turns.jsonl` carries the agent's prose. Untracked it dirties every
# `git status`; committed by accident it carries that prose into someone else's PR. Same bar
# as every other file init touches, including byte-for-byte restore on uninstall.


def _gitignore(repo: Path) -> Path:
    return repo / ".gitignore"


def test_init_ignores_tycho_state(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert ".tycho/" in _gitignore(repo).read_text().splitlines()


def test_init_keeps_every_existing_gitignore_line(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _gitignore(repo).write_text("node_modules/\n*.log\n")
    init_mod.init(repo, only="claude", assume_yes=True)
    lines = _gitignore(repo).read_text().splitlines()
    assert lines[:2] == ["node_modules/", "*.log"]  # theirs first, untouched
    assert ".tycho/" in lines


def test_init_does_not_glue_onto_a_file_with_no_trailing_newline(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _gitignore(repo).write_text("*.log")  # no trailing newline
    init_mod.init(repo, only="claude", assume_yes=True)
    assert "*.log" in _gitignore(repo).read_text().splitlines()


def test_init_is_idempotent_on_the_gitignore(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    first = _gitignore(repo).read_text()
    init_mod.init(repo, only="claude", assume_yes=True)
    assert _gitignore(repo).read_text() == first
    assert first.count(".tycho/") == 1


def test_init_says_nothing_when_tycho_is_already_ignored(tmp_path: Path):
    """Including via a form we didn't write — we ask git, not the file."""
    repo = _init_repo(tmp_path)
    _gitignore(repo).write_text(".tycho\n")  # their spelling, no slash
    lines = init_mod.init(repo, only="claude", assume_yes=True)
    assert not any("ignored" in ln for ln in lines)
    assert _gitignore(repo).read_text() == ".tycho\n"  # left exactly alone


def test_uninstall_restores_the_gitignore_byte_for_byte(tmp_path: Path):
    repo = _init_repo(tmp_path)
    before = "node_modules/\n*.log\n"
    _gitignore(repo).write_text(before)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert _gitignore(repo).read_text() != before
    init_mod.uninstall(repo, only="claude")
    assert _gitignore(repo).read_text() == before


def test_uninstall_removes_a_gitignore_that_was_only_ours(tmp_path: Path):
    repo = _init_repo(tmp_path)
    assert not _gitignore(repo).exists()
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.uninstall(repo, only="claude")
    assert not _gitignore(repo).exists()  # litter, not politeness


def test_no_gitignore_outside_a_git_repo(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    assert not (tmp_path / ".gitignore").exists()


def test_a_refused_commit_hook_does_not_cost_the_gitignore_entry(tmp_path: Path, monkeypatch):
    """The two git steps are independent — one refusing must not skip the other."""
    repo = _init_repo(tmp_path)

    def _refuse(_repo):
        raise init_mod.ConfigRefused("nope")

    monkeypatch.setattr(init_mod, "_install_git_hook", _refuse)
    lines = init_mod.init(repo, only="claude", assume_yes=True)
    assert any(init_mod.REFUSED in ln for ln in lines)
    assert ".tycho/" in _gitignore(repo).read_text().splitlines()


# --- the adversarial install: nine ways a real machine bites back ------------
#
# Every case was reproduced against a released build before it was fixed. init runs against
# files the developer owns, so the worst outcome is not "Tycho didn't install" — it is
# "Tycho changed something the developer can't get back".


def _statusline(repo: Path) -> dict:
    return json.loads((repo / CLAUDE).read_text()).get("statusLine")


def test_a_second_init_does_not_forget_the_statusline_it_wrapped(tmp_path: Path):
    # init; init; uninstall must still restore the user's badge. The second init used to
    # clear the compose record, and by then the .bak held only Tycho's own line.
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-badge"}}))
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    init_mod.uninstall(tmp_path, only="claude")
    assert _statusline(tmp_path)["command"] == "my-badge"


def test_uninstall_restores_every_key_of_a_wrapped_statusline(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    original = {"type": "command", "command": "my-badge", "padding": 1}
    settings.write_text(json.dumps({"statusLine": original}))
    init_mod.init(tmp_path, only="claude", assume_yes=True)
    init_mod.uninstall(tmp_path, only="claude")
    assert _statusline(tmp_path) == original  # padding is theirs, not ours to drop


@pytest.mark.parametrize("hooks", [
    {"Stop": "echo hi"},                                   # a string, iterated per character
    {"Stop": {"matcher": "*", "hooks": [{"command": "x"}]}},  # a group where a list belongs
    {"Stop": [{"hooks": {"type": "command"}}]},            # a dict where the entry list belongs
    ["a", "b"],                                            # `hooks` itself is not an object
])
def test_a_hooks_shape_we_cannot_parse_is_refused(tmp_path: Path, hooks):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    original = json.dumps({"hooks": hooks}) + "\n"
    settings.write_text(original)
    lines = init_mod.init(tmp_path, only="claude", assume_yes=True)
    assert any(init_mod.REFUSED in ln for ln in lines), lines
    assert settings.read_text() == original  # valid JSON, unknown shape: still don't guess


def test_a_hooks_shape_we_cannot_parse_is_refused_on_uninstall_too(tmp_path: Path):
    settings = tmp_path / CLAUDE
    settings.parent.mkdir(parents=True)
    original = json.dumps({"hooks": {"Stop": "echo hi"}}) + "\n"
    settings.write_text(original)
    lines = init_mod.uninstall(tmp_path, only="claude")
    assert any(init_mod.REFUSED in ln for ln in lines), lines
    assert settings.read_text() == original


def test_a_hooks_path_outside_the_repo_is_refused(tmp_path: Path, monkeypatch):
    # A global `core.hooksPath` makes `--git-path hooks` answer with a path shared by every
    # repo, so a repo-local init becomes machine-wide and the next uninstall strips them all.
    shared = tmp_path / "githooks"
    shared.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[core]\n\thooksPath = {shared.as_posix()}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = _init_repo(tmp_path)

    lines = init_mod.init(repo, only="claude", assume_yes=True)

    assert not (shared / HOOK).exists(), "a repo-local init must not write a machine-wide hook"
    assert any(init_mod.REFUSED in ln and "core.hooksPath" in ln for ln in lines), lines
    assert (repo / CLAUDE).exists()  # …and the refusal doesn't take the harness install down


@posix_only
def test_init_does_not_make_a_foreign_inert_hook_executable(tmp_path: Path):
    # A non-executable hook is not run by git at all. chmod +x on someone else's dead
    # `exit 1` hook would make it live — Tycho breaking `git commit` on its way in.
    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o644)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert not (hook.stat().st_mode & 0o111), "we don't decide their hook should start running"


def test_uninstalling_one_harness_keeps_the_shared_git_integration(tmp_path: Path):
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.uninstall(repo, only="cursor")  # a harness that was never wired here
    assert _hook_path(repo).is_file(), "the claude install still needs the commit trailer"
    assert ".tycho/" in _gitignore(repo).read_text().splitlines()


def test_a_damaged_fence_is_repaired_not_duplicated(tmp_path: Path):
    # Someone deletes the closing marker by hand. Re-init must repair the block, not stack a
    # second — uninstall takes out only one, leaving an orphan firing forever.
    repo = _init_repo(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    hook = _hook_path(repo)
    damaged = "\n".join(ln for ln in hook.read_text().splitlines()
                        if ln.strip() != init_mod._GIT_END) + "\necho theirs\n"
    hook.write_text(damaged)

    init_mod.init(repo, only="claude", assume_yes=True)

    text = hook.read_text()
    assert text.count(init_mod._GIT_BEGIN) == 1
    assert "echo theirs" in text  # repairing our block is not licence to eat their lines
    init_mod.uninstall(repo, only="claude")
    assert init_mod.attest_command() not in hook.read_text()


def test_uninstall_restores_a_gitignore_that_had_no_trailing_newline(tmp_path: Path):
    repo = _init_repo(tmp_path)
    before = "dist"  # no trailing newline — the docstring promises byte-for-byte
    _gitignore(repo).write_text(before)
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.uninstall(repo, only="claude")
    assert _gitignore(repo).read_text() == before


def test_a_clean_uninstall_leaves_no_backup_files(tmp_path: Path):
    repo = _init_repo(tmp_path)
    settings = repo / CLAUDE
    settings.write_text(json.dumps({"model": "opus"}))
    _gitignore(repo).write_text("*.log\n")
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho theirs\n")

    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.uninstall(repo, only="claude")

    left = [p.name for p in (settings.parent, repo, hook.parent) for p in p.glob("*.tycho.bak")]
    assert left == [], f"a clean uninstall leaves nothing behind: {left}"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook through a POSIX /bin/sh")
def test_a_commit_still_succeeds_when_the_hook_runs_under_set_u(tmp_path: Path):
    # `set -eu` is ordinary hygiene, and git passes no `$2` for an editor commit. Under
    # `set -u` the unset expansion kills the shell before our `|| :`, so the fail-open guard
    # doesn't hold. `git commit -m` hides it — that one does pass `$2`.
    from conftest import git

    repo = _init_repo(tmp_path)
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh -eu\n# theirs\n")
    hook.chmod(0o755)
    init_mod.init(repo, only="claude", assume_yes=True)

    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\necho one > "$1"\n')
    editor.chmod(0o755)
    (repo / "a.txt").write_text("hi\n")
    git(repo, "add", "a.txt")
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "commit"],
        capture_output=True, text=True,
        # No global config: a `commit.template` there would hand the hook a `$2` of its own
        # and hide the very thing this test is about.
        env={**os.environ, "GIT_EDITOR": str(editor), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-none")},
    )
    assert done.returncode == 0, done.stderr
    assert git(repo, "log", "--oneline").stdout.strip(), "the commit must exist"


def test_a_tracked_hook_is_refused(tmp_path: Path):
    # husky keeps `.husky/prepare-commit-msg` in the repo. Rewriting it would commit this
    # machine's absolute python path for everyone on the team.
    from conftest import git

    repo = _init_repo(tmp_path)
    husky = repo / ".husky"
    husky.mkdir()
    original = "#!/bin/sh\necho theirs\n"
    (husky / HOOK).write_text(original)
    git(repo, "config", "core.hooksPath", ".husky")
    git(repo, "add", ".husky")
    git(repo, "commit", "-qm", "husky")

    lines = init_mod.init(repo, only="claude", assume_yes=True)

    assert (husky / HOOK).read_text() == original
    assert any(init_mod.REFUSED in ln and "tracked by git" in ln for ln in lines), lines
    assert (repo / CLAUDE).exists()  # the harness install still lands


@posix_only
def test_a_symlinked_hook_is_refused(tmp_path: Path):
    repo = _init_repo(tmp_path)
    real = tmp_path / "elsewhere-hook.sh"
    real.write_text("#!/bin/sh\necho theirs\n")
    hook = _hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.symlink_to(real)

    lines = init_mod.init(repo, only="claude", assume_yes=True)

    assert real.read_text() == "#!/bin/sh\necho theirs\n"
    assert hook.is_symlink()
    assert any(init_mod.REFUSED in ln and "symlink" in ln for ln in lines), lines

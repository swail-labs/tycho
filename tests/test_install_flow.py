"""`tycho install` / `off` / `on` — the one-command setup and its per-repo escape hatch.

The contract being pinned here is mostly about *restraint*: a machine-wide install runs
everywhere, so what it must NOT do matters more than what it does. It must not edit a tracked
file in a repo nobody opted in, must not keep verifying a repo the user switched off, and must
not silently reverse what `uninstall` meant for everyone upgrading from 0.1.x.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tycho import cli
from tycho.store import state
from tycho.wire import hook as hook_mod
from tycho.wire import install as init_mod

CLAUDE = Path(".claude/settings.json")


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch) -> Path:
    """A fake `~/.claude`. Nothing here may touch the developer's real one."""
    home = tmp_path / "userhome" / ".claude"
    home.mkdir(parents=True)
    monkeypatch.setattr(init_mod.harness_mod, "home", lambda name: home.parent / f".{name}")
    return home


@pytest.fixture
def git_home(tmp_path: Path, monkeypatch) -> Path:
    """An isolated global git config + excludes file, so nothing touches the developer's."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconfig"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    return tmp_path / "xdgconfig" / "git" / "ignore"


def repo_at(tmp_path: Path, name: str = "gitrepo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    (repo / ".claude").mkdir(exist_ok=True)
    return repo


# --- install writes the hooks and the one global ignore line ----------------


def test_install_wires_the_user_level_config(user_home: Path, git_home: Path):
    init_mod.install(confirm=lambda: True)
    data = json.loads((user_home / "settings.json").read_text())
    command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert init_mod._is_tycho_hook(command)
    assert init_mod.global_installed() is True


def test_install_adds_one_line_to_the_global_git_ignore(user_home: Path, git_home: Path):
    init_mod.install(confirm=lambda: True)
    assert ".tycho/" in git_home.read_text().splitlines()


def test_that_line_is_what_keeps_tycho_out_of_every_repo(tmp_path: Path, user_home, git_home):
    """The whole point. With `.tycho/` globally ignored, the per-repo `.gitignore` step
    short-circuits on `git check-ignore` — so a machine-wide install never edits a tracked
    file in a repo the user never opted in."""
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    assert init_mod._install_gitignore(repo) is None
    assert not (repo / ".gitignore").exists()


def test_a_repo_stays_clean_under_a_machine_wide_install(tmp_path: Path, user_home, git_home):
    """End to end: `.tycho/` exists and `git status` shows nothing."""
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    (repo / ".tycho").mkdir()
    (repo / ".tycho" / "turns.jsonl").write_text("{}\n")
    out = subprocess.run(["git", "-C", str(repo), "status", "--short"],
                         capture_output=True, text=True).stdout
    assert out.strip() == "", f"expected a clean tree, got: {out!r}"


def test_install_is_idempotent(user_home: Path, git_home: Path):
    init_mod.install(confirm=lambda: True)
    before = git_home.read_text()
    init_mod.install(confirm=lambda: True)
    assert git_home.read_text() == before


def test_declining_writes_nothing(user_home: Path, git_home: Path):
    lines = init_mod.install(confirm=lambda: False)
    assert not (user_home / "settings.json").exists()
    assert not git_home.exists()
    assert any("nothing written" in ln for ln in lines)


def test_uninstall_takes_the_global_ignore_line_back_out(user_home: Path, git_home: Path):
    git_home.parent.mkdir(parents=True, exist_ok=True)
    git_home.write_text("*.log\nbuild/\n")
    init_mod.install(confirm=lambda: True)
    init_mod.uninstall_global()
    assert git_home.read_text() == "*.log\nbuild/\n", "must restore byte-for-byte"


def test_install_respects_a_configured_excludesfile(tmp_path: Path, user_home, git_home, monkeypatch):
    """A user who set `core.excludesFile` gets their file appended to, not a second one
    created that git would never read."""
    theirs = tmp_path / "mine" / "ignore"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("node_modules/\n")
    subprocess.run(["git", "config", "--global", "core.excludesFile", str(theirs)], check=True)
    init_mod.install(confirm=lambda: True)
    assert ".tycho/" in theirs.read_text().splitlines()
    assert not git_home.exists()


def test_no_defaults_can_decline_the_ignore_line_and_still_install(user_home, git_home):
    lines = init_mod.install(confirm=lambda: True, ignore_confirm=lambda: False)
    assert init_mod.global_installed() is True
    assert not git_home.exists()
    assert any(".gitignore" in ln for ln in lines), "must say what happens instead"


# --- off / on ---------------------------------------------------------------


def test_off_stops_the_hook_from_touching_the_repo_at_all(tmp_path: Path, user_home, git_home):
    """Not just "renders no verdict" — a repo switched off must see no `.tycho/` appear,
    or `off` leaves a directory behind and reads as a lie."""
    repo = repo_at(tmp_path)
    init_mod.off(repo)
    assert hook_mod._skip(repo) is True
    payload = json.dumps({"cwd": str(repo), "hook_event_name": "Stop"})
    assert hook_mod.run(payload) is None
    assert not (repo / ".tycho").exists()


def test_on_reverses_off(tmp_path: Path, user_home, git_home):
    repo = repo_at(tmp_path)
    init_mod.off(repo)
    init_mod.on(repo)
    assert state.excluded(repo) is False
    assert hook_mod._skip(repo) is False


def test_off_is_per_repo_not_machine_wide(tmp_path: Path, user_home, git_home):
    a, b = repo_at(tmp_path, "a"), repo_at(tmp_path, "b")
    init_mod.off(a)
    assert hook_mod._skip(a) is True
    assert hook_mod._skip(b) is False


def test_off_removes_a_repo_local_install_too(tmp_path: Path, user_home, git_home):
    """One meaning — leave this repo alone — however it happens to be wired."""
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert init_mod.wired_here(repo) is True
    init_mod.off(repo)
    assert init_mod.wired_here(repo) is False
    assert state.excluded(repo) is True


def test_off_leaves_config_and_record_alone_without_purge(tmp_path: Path, user_home, git_home):
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.off(repo)
    assert (repo / ".tycho.toml").is_file(), "may be committed and shared — not ours to delete"


def test_off_purge_deletes_the_repo_local_artifacts(tmp_path: Path, user_home, git_home):
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    init_mod.off(repo, purge=True)
    assert not (repo / ".tycho.toml").exists()
    assert not (repo / ".tycho").exists()


def test_init_re_enables_a_repo_that_was_switched_off(tmp_path: Path, user_home, git_home):
    """`tycho off` then `tycho init` must mean what it reads like, or the install "succeeds"
    and nothing ever verifies."""
    repo = repo_at(tmp_path)
    init_mod.off(repo)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert state.excluded(repo) is False


def test_unreadable_state_never_silences_the_verifier(tmp_path: Path, monkeypatch):
    """A verifier that goes quiet because it couldn't read a file is the worst failure here,
    so every error path in the skip check answers "keep verifying"."""
    monkeypatch.setattr(state, "excluded", lambda repo: (_ for _ in ()).throw(OSError("boom")))
    assert hook_mod._skip(tmp_path) is False


# --- TYCHO_AUTO -------------------------------------------------------------


def test_auto_off_skips_repos_that_never_ran_init(tmp_path: Path, monkeypatch, user_home, git_home):
    monkeypatch.setenv("TYCHO_AUTO", "0")
    assert hook_mod._skip(repo_at(tmp_path)) is True


def test_auto_off_still_honours_a_repo_that_asked_for_it(tmp_path: Path, monkeypatch, user_home, git_home):
    """A machine-wide switch must not silently undo a per-repo decision."""
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    monkeypatch.setenv("TYCHO_AUTO", "0")
    assert hook_mod._skip(repo) is False


# --- the uninstall inversion ------------------------------------------------


def test_uninstall_refuses_to_guess_in_a_repo_with_its_own_install(tmp_path, monkeypatch, capsys, user_home):
    """`uninstall` used to mean this repo and now means this machine. That inversion lands on
    every 0.1.x user, where every repo has its own install — so it refuses rather than
    offering a default, and names both ways out."""
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    monkeypatch.chdir(repo)
    assert cli.main(["uninstall"]) == cli.ExitCode.USAGE
    err = capsys.readouterr().err
    assert "tycho off" in err and "--here" in err and "--global" in err


def test_uninstall_here_removes_only_this_repo(tmp_path, monkeypatch, user_home, git_home):
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    monkeypatch.chdir(repo)
    assert cli.main(["uninstall", "--here"]) == cli.ExitCode.OK
    # The settings file survives — it may hold settings Tycho never wrote. Only our entries go.
    assert "tycho" not in (repo / CLAUDE).read_text()
    assert init_mod.wired_here(repo) is False
    assert init_mod.global_installed() is True, "the machine-wide install is untouched"


def test_bare_uninstall_is_machine_wide_where_there_is_nothing_local(tmp_path, monkeypatch, user_home, git_home):
    init_mod.install(confirm=lambda: True)
    monkeypatch.chdir(repo_at(tmp_path))
    assert cli.main(["uninstall"]) == cli.ExitCode.OK
    assert init_mod.global_installed() is False


def test_the_old_global_flag_still_works(tmp_path, monkeypatch, user_home, git_home):
    """Scripts written against 0.2.0 pass `--global`; it must keep meaning what it meant."""
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    monkeypatch.chdir(repo)
    assert cli.main(["uninstall", "--global"]) == cli.ExitCode.OK
    assert init_mod.global_installed() is False


def test_init_global_is_still_accepted_as_the_old_spelling(user_home: Path, git_home: Path):
    init_mod.init_global(confirm=lambda: True)
    assert init_mod.global_installed() is True


# --- the first-seen notice --------------------------------------------------


def test_a_repo_is_told_once_that_tycho_is_verifying_it(tmp_path: Path, user_home, git_home):
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    first = hook_mod._first_seen(repo)
    assert first and "verifying this repo" in first[0]
    assert "tycho off" in first[0], "must carry the way out, not just the way in"
    assert hook_mod._first_seen(repo) == [], "once per repo, never per turn"


def test_nothing_offers_to_set_up_a_repo_it_is_already_verifying(tmp_path, user_home, git_home):
    """`doctor` on a switched-off repo printed "Tycho isn't set up here — run `tycho init`"
    directly above "verification is OFF" — two lines contradicting each other, one of them
    telling the user to undo what they had just asked for."""
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    assert init_mod.offer_first_run(repo, confirm=lambda _: False) == []
    init_mod.off(repo)
    assert init_mod.offer_first_run(repo, confirm=lambda _: False) == []


def test_a_repo_that_ran_init_is_not_told(tmp_path: Path, user_home, git_home):
    """They set it up themselves — announcing it is noise."""
    init_mod.install(confirm=lambda: True)
    repo = repo_at(tmp_path)
    init_mod.init(repo, only="claude", assume_yes=True)
    assert hook_mod._first_seen(repo) == []


def test_nothing_is_announced_without_a_machine_wide_install(tmp_path: Path, user_home, git_home):
    """The sentence names a machine-wide install, so it must not be said where there is none.
    Caught by CI, not locally: this repo has a real install, so `repo_root` resolving to it
    suppressed the notice and the whole class of case went untested."""
    assert hook_mod._first_seen(repo_at(tmp_path)) == []


# --- the repo-root fallback -------------------------------------------------


def test_the_first_write_in_a_fresh_repo_anchors_at_the_git_root(tmp_path: Path):
    """Under a machine-wide install a repo has no `.tycho/` until the first write, and an
    agent that ran `cd packages/slug` reports that subdirectory as its cwd. Anchoring there
    would open the ledger in the wrong place — permanently, since every later turn would find
    it there."""
    repo = repo_at(tmp_path)
    sub = repo / "packages" / "slug"
    sub.mkdir(parents=True)
    assert state.root_for(sub) == repo

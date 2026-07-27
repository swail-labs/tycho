"""The commit trailer (strategy §6.6): what it covers, that it verifies, and that it can
never, under any circumstance, break `git commit`.

Two halves. The first drives `attest.py` directly over a synthetic record — format,
determinism, the never-verified case, the three-valued verification. The second makes **real
commits in a real throwaway repo** with the hook actually installed, because the failure this
feature must not have (a hook that fails a commit) only exists at the git level: no amount of
unit testing of `write_message` proves `git commit` still returns 0 when Tycho is broken.

Every git repo here is built under `tmp_path` with `-c user.email=…` on the command line, so
nothing reads or writes the developer's real git config, and `harness_mod.home` is redirected
so nothing reads their real `~/.claude`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tycho import attest
from tycho import harness as harness_mod
from tycho import init as init_mod
from tycho import record as record_mod

MSG = "commit-message.txt"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    """No test here may see a real `~/.claude` — `init` reads it to detect a global install."""
    monkeypatch.setattr(
        harness_mod, "home", lambda name: tmp_path_factory.mktemp("harness-home") / f".{name}"
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, self-contained git repo with one commit — the thing every path here reads."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "seed.txt").write_text("seed\n")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


def _record(repo: Path, *paths: str, verdict: str = "VERIFIED", ended_at: float = 1000.0) -> dict:
    """Append a minimal but schema-shaped turn record touching `paths`."""
    row = {
        "schema": record_mod.SCHEMA,
        "id": f"{abs(hash((paths, verdict, ended_at))):016x}"[:16],
        "session": "s1",
        "harness": "claude",
        "model": "opus",
        "agent_version": "1.0",
        "started_at": ended_at - 10,
        "ended_at": ended_at,
        "verdict": verdict,
        "stage": "claim_supported",
        "checks": [],
        "files": [{"path": p, "kind": "edit", "ts": ended_at} for p in paths],
        "commands": [],
        "claims": ["did the thing"],
    }
    record_mod.append(repo, row)
    return row


# --- what the attestation covers ---------------------------------------------


def test_attestation_covers_every_turn_that_touched_the_commits_files(repo: Path):
    # The whole point of not digesting a single record: one commit, several turns.
    _record(repo, "a.py")
    _record(repo, "a.py", "b.py", ended_at=1100.0)
    _record(repo, "unrelated.py", ended_at=1200.0)  # touched nothing in this commit

    body = attest.attestation(repo, ["a.py"])

    assert len(body["turns"]) == 2
    assert body["verdicts"] == {"VERIFIED": 2}


def test_attestation_is_deterministic_and_order_independent_in_its_digest(repo: Path):
    _record(repo, "a.py")
    _record(repo, "a.py", verdict="STALE", ended_at=1100.0)

    first = attest.attestation(repo, ["a.py"])
    second = attest.attestation(repo, ["a.py"])

    assert first == second
    assert record_mod.digest(first) == record_mod.digest(second)
    # Reproducible from the record alone: rebuilding the body by hand hashes identically.
    assert record_mod.digest({**first}) == record_mod.digest(first)


def test_attestation_is_none_when_no_turn_touched_the_commit(repo: Path):
    _record(repo, "elsewhere.py")
    assert attest.attestation(repo, ["a.py"]) is None
    assert attest.attestation(repo, []) is None


def test_attestation_ignores_turns_recorded_after_the_commit(repo: Path):
    """Without the upper bound, a turn recorded *later* would join the set and every old
    trailer would stop verifying. This is the invariant that makes verification possible."""
    _record(repo, "a.py", ended_at=1000.0)
    _record(repo, "a.py", ended_at=9000.0)

    assert len(attest.attestation(repo, ["a.py"], until=2000.0)["turns"]) == 1
    assert len(attest.attestation(repo, ["a.py"])["turns"]) == 2


def test_a_corrupt_record_line_is_skipped_not_fatal(repo: Path):
    _record(repo, "a.py")
    with record_mod.path_for(repo).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    assert len(attest.attestation(repo, ["a.py"])["turns"]) == 1


# --- the trailer line --------------------------------------------------------


def test_trailer_line_carries_the_digest_and_a_readable_verdict_tally(repo: Path):
    _record(repo, "a.py")
    _record(repo, "a.py", verdict="STALE", ended_at=1100.0)

    body = attest.attestation(repo, ["a.py"])
    line = attest.trailer_line(body)

    assert line.startswith("Tycho-Attestation: sha256:")
    assert record_mod.digest(body) in line
    assert "(2 turns, 1 VERIFIED, 1 STALE)" in line
    assert attest.NEVER not in line


def test_a_commit_no_turn_ever_verified_says_so_out_loud(repo: Path):
    """The stated solo value (§6.6): six months later, tell which commits were agent-written
    and never verified by anything. That case must be *representable*, not omitted."""
    _record(repo, "a.py", verdict="UNSUPPORTED")
    _record(repo, "a.py", verdict="INDETERMINATE", ended_at=1100.0)

    line = attest.trailer_line(attest.attestation(repo, ["a.py"]))

    assert "NEVER VERIFIED: 1 INDETERMINATE, 1 UNSUPPORTED" in line
    assert line.startswith("Tycho-Attestation: sha256:")  # still a real, checkable digest


def test_one_turn_is_singular(repo: Path):
    _record(repo, "a.py")
    assert "(1 turn, 1 VERIFIED)" in attest.trailer_line(attest.attestation(repo, ["a.py"]))


def test_trailer_reads_the_staged_diff(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    assert attest.trailer(repo).startswith("Tycho-Attestation: sha256:")


def test_a_merge_commit_gets_no_trailer(repo: Path):
    """A merge carries no work of its own — its content was attested on the merged commits."""
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    assert attest.trailer(repo, source="merge") is None


def test_no_trailer_in_a_repo_with_no_records(repo: Path):
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    assert attest.trailer(repo) is None


def test_no_trailer_outside_a_git_repo(tmp_path: Path):
    _record(tmp_path, "a.py")
    assert attest.trailer(tmp_path) is None  # nothing staged, nothing to attest — no crash


# --- writing it into the message ---------------------------------------------


def _staged(repo: Path) -> None:
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")


def test_write_message_appends_the_trailer_after_a_blank_line(repo: Path, tmp_path: Path):
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("feat: add a\n\nsome body prose\n")

    assert attest.write_message(repo, msg) is True

    lines = msg.read_text().splitlines()
    assert lines[:3] == ["feat: add a", "", "some body prose"]
    assert lines[3] == ""
    assert lines[4].startswith("Tycho-Attestation:")


def test_write_message_is_idempotent_and_replaces_rather_than_stacks(repo: Path, tmp_path: Path):
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("feat: add a\n")

    attest.write_message(repo, msg)
    first = msg.read_text()
    assert attest.write_message(repo, msg) is False  # nothing changed, nothing rewritten
    assert msg.read_text() == first
    assert first.count("Tycho-Attestation:") == 1


def test_write_message_joins_an_existing_trailer_block(repo: Path, tmp_path: Path):
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("feat: add a\n\nSigned-off-by: Someone <s@example.com>\n")

    attest.write_message(repo, msg)

    lines = msg.read_text().splitlines()
    assert lines[-2] == "Signed-off-by: Someone <s@example.com>"  # no blank wedged between
    assert lines[-1].startswith("Tycho-Attestation:")


def test_a_conventional_commit_subject_is_not_mistaken_for_a_trailer(repo: Path, tmp_path: Path):
    """`fix: tweak` is exactly trailer-shaped. Joining it would glue the attestation onto the
    subject line, and that subject is what `git log --oneline` shows forever. Caught for real
    in a live repo, not in theory."""
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("fix: tweak\n")

    attest.write_message(repo, msg)

    lines = msg.read_text().splitlines()
    assert lines[0] == "fix: tweak"
    assert lines[1] == "", "the subject always gets its separating blank line"
    assert lines[2].startswith("Tycho-Attestation:")


def test_write_message_stays_above_gits_comment_block(repo: Path, tmp_path: Path):
    """git strips the `#` block and the scissors; a trailer written below them is a trailer
    that never reaches the commit."""
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("feat: add a\n\n# Please enter the commit message…\n# On branch main\n")

    attest.write_message(repo, msg)

    lines = msg.read_text().splitlines()
    assert lines[-1] == "# On branch main"
    assert any(ln.startswith("Tycho-Attestation:") for ln in lines)
    assert lines.index("# Please enter the commit message…") > [
        i for i, ln in enumerate(lines) if ln.startswith("Tycho-Attestation:")
    ][0]


def test_an_empty_editor_message_keeps_room_for_the_subject(repo: Path, tmp_path: Path):
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text("\n# Please enter the commit message…\n")

    attest.write_message(repo, msg)

    lines = msg.read_text().splitlines()
    # blank, blank, trailer — a subject typed on line 1 still gets its separating blank line.
    assert lines[0] == "" and lines[1] == ""
    assert lines[2].startswith("Tycho-Attestation:")


def test_write_message_never_raises_and_leaves_the_message_alone_on_failure(
    repo: Path, tmp_path: Path
):
    _record(repo, "a.py")
    _staged(repo)
    missing = tmp_path / "nope" / MSG
    assert attest.write_message(repo, missing) is False  # unreadable → no trailer, no raise

    msg = tmp_path / MSG
    msg.write_text("feat: add a\n")
    assert attest.write_message(tmp_path / "not-a-repo", msg) is False
    assert msg.read_text() == "feat: add a\n"  # untouched


def test_a_stale_trailer_is_kept_when_there_is_nothing_to_replace_it_with(
    repo: Path, tmp_path: Path
):
    """A pruned record must degrade to "stale trailer", never to a silently dropped one —
    dropping it would erase the only evidence the commit was ever attested."""
    msg = tmp_path / MSG
    msg.write_text("feat: add a\n\nTycho-Attestation: sha256:" + "0" * 64 + "\n")
    before = msg.read_text()

    assert attest.write_message(repo, msg) is False
    assert msg.read_text() == before


# --- verification ------------------------------------------------------------


def _commit(repo: Path, path: str, message: str, *extra: str) -> None:
    (repo / path).write_text(f"# {message}\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", message, *extra)


def test_verify_says_yes_when_the_trailer_matches_the_record(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    line = attest.trailer(repo)
    _git(repo, "commit", "-qm", f"feat: add a\n\n{line}")

    ok, said = attest.verify(repo)

    assert ok is True, said
    assert "VERIFIED against the record" in said and "1 turn, 1 VERIFIED" in said


def test_verify_says_no_when_the_trailer_was_tampered_with(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "feat: add a\n\nTycho-Attestation: sha256:" + "0" * 64)

    ok, said = attest.verify(repo)

    assert ok is False
    assert "does NOT match the record" in said


def test_verify_cannot_tell_when_there_is_no_trailer(repo: Path):
    """An unattested commit is ordinary history, not a forgery — and must never read as one."""
    _commit(repo, "a.py", "a human wrote this")

    ok, said = attest.verify(repo)

    assert ok is None
    assert "no Tycho attestation" in said


def test_verify_cannot_tell_when_the_record_is_gone(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", f"feat: add a\n\n{attest.trailer(repo)}")
    record_mod.path_for(repo).unlink()  # the `--purge` / pruned / fresh-clone case

    ok, said = attest.verify(repo)

    assert ok is None
    assert "cannot confirm" in said


def test_verify_on_a_missing_ref_is_unknown_not_a_crash(repo: Path):
    ok, said = attest.verify(repo, "deadbeef")
    assert ok is None and "no such commit" in said


def test_claimed_digest_takes_the_last_trailer():
    msg = f"x\n\nTycho-Attestation: sha256:{'1' * 64}\nTycho-Attestation: sha256:{'2' * 64}\n"
    assert attest.claimed_digest(msg) == "sha256:" + "2" * 64
    assert attest.claimed_digest("no trailer here") is None


# --- the CLI entrypoint (what the git hook actually calls) -------------------


def test_main_write_always_exits_zero_even_with_a_broken_repo(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert attest.main(["--write", str(tmp_path / "does-not-exist")]) == 0
    assert attest.main([str(tmp_path / "does-not-exist"), "message"]) == 0


def test_main_verify_exits_one_only_on_a_real_mismatch(repo: Path, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    _commit(repo, "a.py", "unattested")
    assert attest.main(["--verify"]) == 0  # unknown is not a failure
    # A real mismatch needs a record that *does* cover the commit — otherwise the honest
    # answer is "cannot confirm", which must stay a 0.
    _record(repo, "b.py")
    (repo / "b.py").write_text("x = 1\n")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-qm", "bogus\n\nTycho-Attestation: sha256:" + "0" * 64)
    assert attest.main(["--verify", "HEAD"]) == 1
    assert "does NOT match" in capsys.readouterr().out


def test_bare_main_prints_the_trailer_for_what_is_staged(repo: Path, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    _record(repo, "a.py")
    _staged(repo)
    assert attest.main([]) == 0
    assert capsys.readouterr().out.startswith("Tycho-Attestation: sha256:")


def test_bare_main_says_so_when_there_is_nothing_to_attest(repo: Path, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert attest.main([]) == 0
    assert "nothing to attest" in capsys.readouterr().out


# --- end to end: real commits, real hook -------------------------------------
#
# Everything above proves the logic. These prove the *product*: that the hook git actually
# runs produces a trailer, survives amend / squash / merge / --no-verify, and — the one that
# matters most — that `git commit` still succeeds when Tycho is broken or absent.


@pytest.fixture
def wired(repo: Path) -> Path:
    """`repo` with the commit-trailer hook installed the way `tycho init` installs it."""
    (repo / ".claude").mkdir()
    init_mod.init(repo, only="claude", assume_yes=True)
    return repo


def _trailer_of(repo: Path, ref: str = "HEAD") -> str | None:
    return attest.claimed_digest(_git(repo, "log", "-1", "--format=%B", ref).stdout)


def test_init_installs_a_hook_git_actually_runs(wired: Path):
    hook = init_mod.git_hooks_dir(wired) / "prepare-commit-msg"
    assert hook.is_file()
    if sys.platform != "win32":
        assert hook.stat().st_mode & 0o111, "git only runs a hook it can execute"


def test_a_real_commit_carries_a_verifiable_trailer(wired: Path):
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")

    assert _trailer_of(wired) is not None
    ok, said = attest.verify(wired)
    assert ok is True, said


def test_amend_replaces_the_trailer_rather_than_stacking_it(wired: Path):
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    first = _trailer_of(wired)
    _record(wired, "a.py", verdict="STALE", ended_at=2000.0)
    (wired / "a.py").write_text("x = 2\n")
    _git(wired, "add", "a.py")
    _git(wired, "commit", "-q", "--amend", "--no-edit")

    body = _git(wired, "log", "-1", "--format=%B").stdout
    assert body.count("Tycho-Attestation:") == 1
    assert _trailer_of(wired) != first  # the second turn is now covered too
    assert "2 turns" in body
    ok, said = attest.verify(wired)
    assert ok is True, said


def test_a_merge_commit_carries_no_trailer(wired: Path):
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    _git(wired, "checkout", "-qb", "side", "HEAD~1")
    _record(wired, "b.py", ended_at=1500.0)
    _commit(wired, "b.py", "feat: add b")
    _git(wired, "checkout", "-q", "main")
    _git(wired, "merge", "-q", "--no-ff", "-m", "merge side", "side")

    assert _git(wired, "rev-list", "--parents", "-n", "1", "HEAD").stdout.count(" ") == 2
    assert _trailer_of(wired) is None, "a merge attests nothing of its own"
    assert _trailer_of(wired, "HEAD^2") is not None  # the merged work kept its trailer


def test_a_squash_merge_attests_the_squashed_work(wired: Path):
    _git(wired, "checkout", "-qb", "side")
    _record(wired, "b.py")
    _commit(wired, "b.py", "feat: add b")
    _git(wired, "checkout", "-q", "main")
    _git(wired, "merge", "-q", "--squash", "side")
    _git(wired, "commit", "-qm", "squashed: add b")

    assert _trailer_of(wired) is not None
    ok, said = attest.verify(wired)
    assert ok is True, said


def test_a_rebase_preserves_the_trailer(wired: Path):
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    before = _trailer_of(wired)
    _git(wired, "checkout", "-qb", "side", "HEAD~1")
    _commit(wired, "c.txt", "unrelated")
    _git(wired, "rebase", "-q", "main")

    assert _trailer_of(wired, "main") == before  # rebase reuses the message verbatim


def test_no_verify_still_attests_because_git_does_not_skip_this_hook(wired: Path):
    """`--no-verify` bypasses `pre-commit` and `commit-msg` — **not** `prepare-commit-msg`.
    Worth pinning rather than assuming: it means rushing past a failing linter doesn't also
    silently drop the attestation, which is the behaviour we want and did not have to build."""
    _record(wired, "a.py")
    (wired / "a.py").write_text("x = 1\n")
    _git(wired, "add", "a.py")
    _git(wired, "commit", "-q", "--no-verify", "-m", "feat: add a")

    assert _trailer_of(wired) is not None
    ok, said = attest.verify(wired)
    assert ok is True, said


def test_a_commit_with_no_records_is_unattested_and_uneventful(wired: Path):
    result = _git(wired, "commit", "-q", "--allow-empty", "-m", "chore: nothing", check=False)
    assert result.returncode == 0
    assert _trailer_of(wired) is None


def test_the_commit_still_succeeds_when_tycho_is_gone(repo: Path, monkeypatch):
    """The load-bearing test. A verifier that can fail `git commit` is uninstalled by
    lunchtime — so point the hook at an interpreter that does not exist and commit anyway."""
    monkeypatch.setattr(init_mod, "attest_command", lambda: "/nonexistent/python -m tycho.attest")
    (repo / ".claude").mkdir()
    init_mod.init(repo, only="claude", assume_yes=True)
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    result = _git(repo, "commit", "-m", "feat: add a", check=False)

    assert result.returncode == 0, f"the hook broke the commit:\n{result.stderr}"
    assert _trailer_of(repo) is None  # no attestation — but the commit landed
    assert result.stderr.strip() == "" or "nonexistent" not in result.stderr


def test_the_commit_still_succeeds_under_set_e_in_a_foreign_hook(repo: Path):
    """Our block is inserted into someone else's script, which may well have `set -e`. A
    non-zero exit from Tycho must not take their commit with it."""
    hooks = init_mod.git_hooks_dir(repo)
    (hooks / "prepare-commit-msg").write_text("#!/bin/sh\nset -e\necho theirs >&2\n")
    (hooks / "prepare-commit-msg").chmod(0o755)
    (repo / ".claude").mkdir()
    init_mod.init(repo, only="claude", assume_yes=True)
    Path(hooks / "prepare-commit-msg").write_text(
        (hooks / "prepare-commit-msg").read_text().replace(
            init_mod.attest_command(), "/nonexistent/python -m tycho.attest"
        )
    )

    result = _git(repo, "commit", "--allow-empty", "-m", "chore: x", check=False)

    assert result.returncode == 0, result.stderr
    assert "theirs" in result.stderr  # and their hook still ran


def test_uninstall_stops_the_trailer_and_a_commit_still_works(wired: Path):
    init_mod.uninstall(wired, only="claude")
    _record(wired, "a.py")
    (wired / "a.py").write_text("x = 1\n")
    _git(wired, "add", "a.py")

    result = _git(wired, "commit", "-m", "feat: add a", check=False)

    assert result.returncode == 0
    assert _trailer_of(wired) is None
    assert not (init_mod.git_hooks_dir(wired) / "prepare-commit-msg").exists()


def test_the_installed_hook_uses_a_path_git_bash_can_resolve():
    """Git runs hooks through its own bundled shell on Windows, which eats unquoted
    backslashes — the same trap `_command_for` exists for."""
    command = init_mod.attest_command()
    assert "\\" not in command
    assert command.startswith('"') or " -m tycho.attest" in command or " attest --write" in command


def test_a_frozen_build_falls_back_to_the_console_script(monkeypatch):
    monkeypatch.setattr(init_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(init_mod.sys, "executable", r"C:\Program Files\tycho\tycho.exe")
    assert init_mod.attest_command() == '"C:/Program Files/tycho/tycho.exe" attest --write'


def test_hook_json_survives_a_round_trip_through_the_settings_file(wired: Path):
    """Sanity: nothing about the git hook disturbed the harness config it ships beside."""
    data = json.loads((wired / ".claude" / "settings.json").read_text())
    assert init_mod._is_tycho_hook(data["hooks"]["Stop"][0]["hooks"][0]["command"])

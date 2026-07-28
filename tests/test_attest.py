"""The commit trailer (strategy §6.6): what it covers, that it verifies, and that it can
never, under any circumstance, break `git commit`.

Two halves. The first drives `attest.py` directly over a synthetic record — format,
determinism, the never-verified case, the three-valued verification. The second makes **real
commits in a real throwaway repo** with the hook actually installed, because the failure this
feature must not have (a hook that fails a commit) only exists at the git level: no amount of
unit testing of `write_message` proves `git commit` still returns 0 when Tycho is broken.

Every git repo here comes from the shared `git_repo` fixture, built under `tmp_path` with its
identity on the command line, so nothing reads or writes the developer's real git config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import git as _git
from conftest import turn_record

from tycho import attest
from tycho import install as init_mod
from tycho import record as record_mod

MSG = "commit-message.txt"


@pytest.fixture
def repo(git_repo: Path) -> Path:
    return git_repo


def _record(repo: Path, *paths: str, verdict: str = "VERIFIED",
            ended_at: float | None = None) -> dict:
    """Append a minimal but schema-shaped turn record touching `paths`.

    Defaults to *now*, not to a fixed epoch: the attestation window starts at the previous
    commit, and the commits these tests make are stamped with the real clock. A turn dated
    1970 is a turn from before the last commit, and correctly excluded."""
    ended_at = time.time() if ended_at is None else ended_at
    row = turn_record(
        id=f"{abs(hash((paths, verdict, ended_at))):016x}"[:16],
        session="s1", model="opus", agent_version="1.0",
        started_at=ended_at - 10, ended_at=ended_at, verdict=verdict,
        files=[{"path": p, "kind": "edit", "ts": ended_at} for p in paths],
        claims=["did the thing"],
    )
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


# `--verify` and the bare trailer print live in `cli._attest`, covered by
# tests/test_cli_surface.py.


# --- end to end: real commits, real hook -------------------------------------
# The hook git actually runs produces a trailer, survives amend/squash/merge/--no-verify,
# and — the one that matters most — `git commit` still succeeds when Tycho is broken.


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
    _record(wired, "a.py", verdict="STALE")
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
    _record(wired, "b.py")
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
    monkeypatch.setattr(init_mod.spelling, "attest_command", lambda: "/nonexistent/python -m tycho.attest")
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


# --- the window: bounded below as well as above ------------------------------


def test_old_green_turns_do_not_bury_todays_failure(wired: Path):
    """Three VERIFIED turns on `src/app.py` from a month ago, then today's FAILED one. Bounded
    only above, the commit reads `(4 turns, 3 VERIFIED, 1 FAILED)` and `NEVER VERIFIED` is
    suppressed — so `git log --grep 'NEVER VERIFIED'` never finds the commit that needs it."""
    old = time.time() - 30 * 86400
    for i in range(3):
        _record(wired, "app.py", ended_at=old + i)
    _commit(wired, "app.py", "feat: last month's work")  # those three are that commit's
    _record(wired, "app.py", verdict="FAILED")

    (wired / "app.py").write_text("# today\n")
    _git(wired, "add", "app.py")
    line = attest.trailer(wired)

    assert "1 turn, NEVER VERIFIED: 1 FAILED" in line
    assert "3 VERIFIED" not in line


def test_the_window_starts_at_the_previous_commit(repo: Path):
    _record(repo, "a.py", ended_at=100.0)      # long before this repo's only commit
    _record(repo, "a.py", verdict="STALE")     # now
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    assert "1 turn" in attest.trailer(repo)


# --- the trailer names its own turns -----------------------------------------


def test_a_pruned_turn_reads_as_cannot_tell_not_as_a_forgery(wired: Path):
    """The module's stated worst outcome. Two turns cover the commit; retention later drops
    one; recomputing the set from paths and timestamps yields a different digest and the
    verifier accuses a perfectly good commit of being forged."""
    first = _record(wired, "a.py")
    _record(wired, "a.py", verdict="STALE")
    _commit(wired, "a.py", "feat: add a")
    assert attest.verify(wired)[0] is True

    kept = [ln for ln in record_mod.path_for(wired).read_text().splitlines()
            if json.loads(ln)["id"] != first["id"]]
    record_mod.path_for(wired).write_text("\n".join(kept) + "\n")

    ok, said = attest.verify(wired)

    assert ok is None, said
    assert "cannot confirm" in said
    assert "does NOT match" not in said


def test_the_trailer_carries_the_ids_of_the_turns_it_covers(repo: Path):
    row = _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    line = attest.trailer(repo)

    assert f"turns={row['id']}" in line
    assert attest.claimed_ids(line) == [row["id"]]


def test_verification_still_works_on_a_trailer_written_before_turn_ids(repo: Path):
    """Old commits keep verifying: no `turns=` means fall back to reconstructing the set."""
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    legacy = attest.trailer(repo).split(" turns=")[0]
    _git(repo, "commit", "-qm", f"feat: add a\n\n{legacy}")

    ok, said = attest.verify(repo)

    assert ok is True, said


def test_a_tampered_digest_is_still_caught_when_the_turns_are_all_there(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    tampered = attest._DIGEST_RE.sub("sha256:" + "b" * 64, attest.trailer(repo))
    _git(repo, "commit", "-qm", f"feat: add a\n\n{tampered}")

    ok, said = attest.verify(repo)

    assert ok is False, said
    assert "does NOT match" in said


# --- bound to this commit, not to a set of paths -----------------------------


def test_a_trailer_copied_onto_another_commit_does_not_verify(wired: Path):
    """The forgery that used to work: the body named turns and nothing else, so a legitimate
    trailer was equally true of any commit touching the same files. Lift it onto a
    hand-written, never-verified commit and it read `attestation VERIFIED`, exit 0."""
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    stolen = [ln for ln in _git(wired, "log", "-1", "--format=%B").stdout.splitlines()
              if ln.startswith("Tycho-Attestation:")][0]

    (wired / "b.py").write_text("# hand written\n")
    _git(wired, "add", "b.py")
    _git(wired, "commit", "-q", "--no-verify", "-m", f"chore: mine\n\n{stolen}")

    ok, said = attest.verify(wired)

    assert ok is not True, said
    assert "VERIFIED against the record" not in said
    assert "cannot confirm" in said


def test_a_revert_does_not_inherit_the_attestation(repo: Path):
    """No hook here on purpose: a revert carries the original message, trailer and all, on
    any machine where Tycho isn't installed to rewrite it."""
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    line = attest.trailer(repo)
    _git(repo, "commit", "-qm", f"feat: add a\n\n{line}")
    assert attest.verify(repo)[0] is True

    _git(repo, "revert", "--no-edit", "-n", "HEAD")
    _git(repo, "commit", "-qm", f"Revert \"feat: add a\"\n\n{line}")

    ok, said = attest.verify(repo)

    assert ok is not True, said
    assert "cannot confirm" in said


def test_the_same_turns_over_different_content_do_not_share_a_digest(repo: Path):
    """Two commits covered by the same turns used to digest byte-identically, which is what
    made a trailer portable between them."""
    _record(repo, "a.py")
    turns = attest.covered(repo, ["a.py"])

    one = record_mod.digest(attest._body(turns, "a" * 40))
    two = record_mod.digest(attest._body(turns, "b" * 40))

    assert one != two


def test_the_trailer_names_the_tree_it_was_written_for(repo: Path):
    _record(repo, "a.py")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")

    line = attest.trailer(repo)

    assert attest.claimed_tree(line) == attest.staged_tree(repo)


# --- git commit -v -----------------------------------------------------------


def test_the_trailer_survives_the_verbose_scissors(repo: Path, tmp_path: Path):
    """`commit.verbose=true` puts the diff below a scissors line, and its body is not
    comment-prefixed — so the trailer was appended *inside the diff* and thrown away with it."""
    _record(repo, "a.py")
    _staged(repo)
    msg = tmp_path / MSG
    msg.write_text(
        "feat: add a\n"
        "# Please enter the commit message…\n"
        "# ------------------------ >8 ------------------------\n"
        "# Do not modify or remove the line above.\n"
        "diff --git a/a.py b/a.py\n"
        "+++ b/a.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )

    attest.write_message(repo, msg)

    lines = msg.read_text().splitlines()
    scissors = next(i for i, ln in enumerate(lines) if ">8" in ln)
    trailer = next(i for i, ln in enumerate(lines) if ln.startswith("Tycho-Attestation:"))
    assert trailer < scissors, "written below the scissors, git discards it"
    assert lines[-1] == "+x = 1", "the diff below is left exactly as it was"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a shell editor")
def test_a_real_verbose_commit_still_carries_a_verifiable_trailer(wired: Path):
    """`commit.verbose` only appends the diff when git opens an editor, so this drives a real
    one — the scissors block has to be there for the bug to exist."""
    _record(wired, "a.py")
    (wired / "a.py").write_text("x = 1\n")
    _git(wired, "add", "a.py")
    editor = wired / "editor.sh"
    editor.write_text('#!/bin/sh\nprintf "feat: add a\\n" | cat - "$1" > "$1.new"'
                      ' && mv "$1.new" "$1"\n')
    editor.chmod(0o755)

    done = subprocess.run(
        ["git", "-C", str(wired), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "-c", "commit.verbose=true", "commit"],
        capture_output=True, text=True,
        # No global config: this needs git's own defaults, not the developer's
        # commit.template or commit.verbose.
        env={**os.environ, "GIT_EDITOR": str(editor),
             "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )

    assert done.returncode == 0, done.stderr
    assert _trailer_of(wired) is not None, "the trailer went below the scissors"
    ok, said = attest.verify(wired)
    assert ok is True, said


# --- the gate ----------------------------------------------------------------


def test_require_verified_fails_a_commit_whose_every_turn_failed(wired: Path):
    """A matching attestation over a red turn is a valid receipt for broken work. As a CI
    gate, exit 0 on `NEVER VERIFIED: 1 FAILED` is decoration."""
    _record(wired, "a.py", verdict="FAILED")
    _commit(wired, "a.py", "feat: add a")

    assert attest.verify(wired)[0] is True
    ok, said = attest.verify(wired, require_verified=True)

    assert ok is False
    assert "no turn ever reached VERIFIED" in said


def test_require_verified_leaves_cannot_tell_alone(repo: Path):
    """It downgrades a True, never an unknown — accusing a commit Tycho can't read is the
    failure mode this whole module exists to avoid."""
    _commit(repo, "a.py", "chore: no attestation here")
    assert attest.verify(repo, require_verified=True)[0] is None


# --- the two clocks and the two encodings ------------------------------------


def test_a_turn_recorded_in_the_same_second_as_the_commit_verifies(wired: Path):
    """The normal agent flow, and the clock-slack asymmetry: the trailer is written before the
    commit exists, the Stop hook records the turn a moment after it lands, and verification
    used to sweep that turn in (commit time + 1s) though the trailer could not have. The
    trailer naming its own turns is what makes the two sides agree."""
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    assert attest.verify(wired)[0] is True

    committed = float(_git(wired, "show", "-s", "--format=%ct", "HEAD").stdout.strip())
    _record(wired, "a.py", verdict="STALE", ended_at=committed + 0.5)  # the Stop hook, after

    ok, said = attest.verify(wired)

    assert ok is True, said
    assert "1 turn" in said, "the turn recorded after the commit is not part of it"


def test_a_non_ascii_path_still_attests_and_verifies(wired: Path):
    """`git show --name-only` renders `café.py` octal-escaped and quoted unless
    core.quotePath is off, which matches nothing the record stores."""
    _record(wired, "café.py")
    _commit(wired, "café.py", "feat: add café")

    assert _trailer_of(wired) is not None
    ok, said = attest.verify(wired)
    assert ok is True, said


def test_amend_with_a_new_message_does_not_report_a_mismatch(wired: Path):
    """`git commit --amend -m` reaches the hook as source=message, so the write side reads a
    different diff base than verify does. Under-covering is survivable; accusing the commit
    of a mismatch is not."""
    _record(wired, "a.py")
    _commit(wired, "a.py", "feat: add a")
    (wired / "a.py").write_text("x = 2\n")
    _git(wired, "add", "a.py")
    _git(wired, "commit", "-q", "--amend", "-m", "feat: add a, better")

    ok, said = attest.verify(wired)

    assert ok is not False, said

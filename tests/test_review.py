"""`tycho review` — hunk parsing, the coverage judgement, ranking, and honest degrading.

Two halves, and they are deliberately separate: `gitstate.parse_hunks` is a pure text
parser (fed raw diffs, including shapes git only emits on a bad day), and
`review.classify` is a pure judgement over the turn record. The end-to-end tests run
against a real temp git repo, because the one thing a fake diff can't check is that git
still emits what the parser expects.

The subtle test in here is `test_command_before_the_edit_does_not_count`: a passing run
that predates the edit proves nothing about it, and getting that backwards is how a
review tool starts lying.
"""

import json
import subprocess
from pathlib import Path

from tycho import gitstate, review

NOW = 1_000_000.0


# --- helpers -----------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def test_a_machine_without_git_degrades_instead_of_raising(tmp_path: Path, monkeypatch):
    """`review.inspect` promises it never raises, and it is reachable from the Stop hook and
    the commit hook. Without the guard in `gitstate._git`, a missing git binary raises
    FileNotFoundError straight through it."""
    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    monkeypatch.setattr(gitstate.subprocess, "run", no_git)
    lines, findings = review.inspect(tmp_path, "HEAD")
    assert findings == []
    assert "not a git repository" in lines[0]


def repo_with_commit(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    (tmp_path / "src.py").write_text("".join(f"line{i}\n" for i in range(1, 31)))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def write_record(repo: Path, files=(), commands=(), started=0.0, ended=0.0, checks=()) -> None:
    """One turn record on disk, in the shape `record.build` writes."""
    path = repo / ".tycho" / "turns.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": 1,
        "id": "0" * 16,
        "started_at": started,
        "ended_at": ended,
        "verdict": "VERIFIED",
        "stage": "claim_supported",
        "checks": [{"name": n, "status": s, "evidence": ""} for n, s in checks],
        "files": [{"path": p, "kind": "edit", "ts": ts} for p, ts in files],
        "commands": [{"cmd": c, "runner": r, "outcome": o} for c, r, o in commands],
        "claims": [],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def hunk(path="src.py", start=1, end=1, status="modified") -> gitstate.Hunk:
    return gitstate.Hunk(path, start, end, 1, 0, status)


def levels(findings) -> list[str]:
    return [f.level for f in findings]


# --- hunk parsing ------------------------------------------------------------


def test_parses_multiple_hunks_to_changed_line_ranges():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "index 111..222 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,5 +1,5 @@\n"
        " one\n"
        " two\n"
        "-three\n"
        "+THREE\n"
        " four\n"
        " five\n"
        "@@ -20,3 +20,4 @@\n"
        " twenty\n"
        "+added\n"
        " twentyone\n"
        " twentytwo\n"
    )
    a, b = gitstate.parse_hunks(diff)
    # Context is trimmed: only the changed lines get addressed.
    assert (a.path, a.start, a.end, a.added, a.removed) == ("a.py", 3, 3, 1, 1)
    assert (b.start, b.end, b.added, b.removed) == (21, 21, 1, 0)
    assert a.ref == "a.py:3" and b.ref == "a.py:21"


def test_ref_renders_a_range():
    assert hunk(path="tycho/state.py", start=88, end=114).ref == "tycho/state.py:88-114"
    assert hunk(start=7, end=7).ref == "src.py:7"
    assert gitstate.Hunk("x.png", 0, 0, 0, 0, "binary").ref == "x.png"


def test_parses_new_deleted_renamed_and_binary_files():
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+x = 1\n"
        "+y = 2\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-a\n"
        "-b\n"
        "diff --git a/old.py b/moved.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to moved.py\n"
        "diff --git a/pic.png b/pic.png\n"
        "Binary files a/pic.png and b/pic.png differ\n"
    )
    new, gone, moved, pic = gitstate.parse_hunks(diff)
    assert (new.path, new.status, new.start, new.end) == ("new.py", "added", 1, 2)
    # A deleted file is addressed on the side that still has line numbers: the old one.
    assert (gone.path, gone.status, gone.start, gone.end) == ("gone.py", "deleted", 1, 2)
    assert (moved.path, moved.status, moved.ref) == ("moved.py", "renamed", "moved.py")
    assert (pic.path, pic.status, pic.ref) == ("pic.png", "binary", "pic.png")


def test_no_trailing_newline_marker_is_not_counted_as_a_line():
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    (h,) = gitstate.parse_hunks(diff)
    assert (h.start, h.end, h.added, h.removed) == (1, 1, 1, 1)


def test_blank_context_line_does_not_end_the_hunk_body():
    # A blank context line is "" once splitlines has eaten the single space some tools
    # strip. The @@ counts are what end the body, so the second change still parses.
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,4 +1,4 @@\n"
        "-one\n"
        "+ONE\n"
        "\n"
        " three\n"
        " four\n"
    )
    (h,) = gitstate.parse_hunks(diff)
    assert (h.start, h.end, h.added, h.removed) == (1, 1, 1, 1)


def test_unknown_hunk_header_degrades_to_a_whole_file_entry():
    # A combined diff (`@@@`, from a merge) has no two-sided line numbers to trust.
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@@ -1,2 -1,2 +1,2 @@@\n"
        "++merged\n"
    )
    (h,) = gitstate.parse_hunks(diff)
    assert h.status == "unparsed" and h.start == 0 and h.ref == "a.py"


def test_parser_is_total_on_junk():
    for junk in ("", "not a diff at all\n", "@@ -1,1 +1,1 @@\n+orphan hunk\n", "\x00\x01"):
        assert isinstance(gitstate.parse_hunks(junk), tuple)


def test_parse_is_bounded_by_limit():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n" + "".join(
        f"@@ -{i},1 +{i},1 @@\n-x\n+y\n" for i in range(1, 500)
    )
    assert len(gitstate.parse_hunks(diff)) == 499
    assert len(gitstate.parse_hunks(diff, limit=10)) == 10


def test_diff_hunks_on_a_real_repo(tmp_path: Path):
    repo = repo_with_commit(tmp_path)
    text = (repo / "src.py").read_text().splitlines()
    text[4] = "CHANGED"
    del text[9:12]
    (repo / "src.py").write_text("\n".join(text) + "\n")
    hunks = gitstate.diff_hunks(repo)
    assert [(h.path, h.start, h.end) for h in hunks] == [("src.py", 5, 10)]
    assert gitstate.untracked(repo) == ()


def test_diff_hunks_reports_unknown_ref_as_none_not_as_clean(tmp_path: Path):
    repo = repo_with_commit(tmp_path)
    assert gitstate.diff_hunks(repo, "nosuchref") is None
    assert gitstate.diff_hunks(repo, "HEAD") == ()


# --- the coverage judgement --------------------------------------------------


def test_source_with_no_recorded_command_is_unexercised(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW)], started=NOW - 10, ended=NOW)
    (f,) = review.classify(tmp_path, [hunk()], now=NOW)
    assert f.level == review.UNEXERCISED
    assert "no passing command in any recorded turn" in f.reason


def test_passing_run_after_the_edit_counts_as_exercised(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW - 100)], started=NOW - 110, ended=NOW - 90,
                 commands=[("pytest -q", True, "passed")])
    (f,) = review.classify(tmp_path, [hunk()], now=NOW)
    assert f.level == review.EXERCISED


def test_command_before_the_edit_does_not_count(tmp_path: Path):
    # The subtle one. Turn 1 ran a green suite; turn 2 edited the file afterwards and ran
    # nothing. The suite passed — and proves exactly nothing about the later edit.
    write_record(tmp_path, files=[("other.py", NOW - 500)], started=NOW - 510, ended=NOW - 490,
                 commands=[("pytest -q", True, "passed")])
    write_record(tmp_path, files=[("src.py", NOW - 60)], started=NOW - 70, ended=NOW - 50)
    (f,) = review.classify(tmp_path, [hunk()], now=NOW)
    assert f.level == review.UNEXERCISED
    assert "no recorded command ran after it" in f.reason


def test_same_turn_run_counts_unless_the_turn_recorded_stale(tmp_path: Path):
    # Commands carry no timestamp of their own, so a same-turn run is credited by default
    # — except when that turn's own test_freshness said a source outran the run. Mirroring
    # the check rather than re-deriving it is what keeps the two from disagreeing.
    write_record(tmp_path, files=[("src.py", NOW - 20)], started=NOW - 30, ended=NOW - 10,
                 commands=[("pytest -q", True, "passed")])
    assert levels(review.classify(tmp_path, [hunk()], now=NOW)) == [review.EXERCISED]

    fresh = tmp_path / "stale"
    write_record(fresh, files=[("src.py", NOW - 20)], started=NOW - 30, ended=NOW - 10,
                 commands=[("pytest -q", True, "passed")],
                 checks=[("test_freshness", "STALE")])
    assert levels(review.classify(fresh, [hunk()], now=NOW)) == [review.UNEXERCISED]


def test_non_runner_command_is_weaker_than_a_test_run(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW - 100)], started=NOW - 110, ended=NOW - 90,
                 commands=[("python app.py", False, "passed")])
    (f,) = review.classify(tmp_path, [hunk()], now=NOW)
    assert f.level == review.UNTESTED
    assert "no test runner" in f.reason


def test_failed_command_is_not_coverage(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW - 100)], started=NOW - 110, ended=NOW - 90,
                 commands=[("pytest -q", True, "failed")])
    assert levels(review.classify(tmp_path, [hunk()], now=NOW)) == [review.UNEXERCISED]


def test_file_no_record_touched_is_unrecorded_not_unexercised(tmp_path: Path):
    write_record(tmp_path, files=[("other.py", NOW)], started=NOW - 10, ended=NOW,
                 commands=[("pytest -q", True, "passed")])
    (f,) = review.classify(tmp_path, [hunk()], now=NOW)
    assert f.level == review.UNRECORDED
    assert f.reason == "no recorded turn touched this file"


def test_changed_test_file_is_its_own_level(tmp_path: Path):
    # A test edited *after* the last green run is the test_provenance shape: that run did
    # not cover this test, so it is not evidence for anything.
    write_record(tmp_path, files=[("src.py", NOW - 500)], started=NOW - 510, ended=NOW - 490,
                 commands=[("pytest -q", True, "passed")])
    write_record(tmp_path, files=[("tests/test_src.py", NOW)], started=NOW - 10, ended=NOW)
    (f,) = review.classify(tmp_path, [hunk(path="tests/test_src.py")], now=NOW)
    assert f.level == review.TEST
    assert "after the last recorded passing run" in f.reason


def test_prose_is_never_flagged_as_uncovered(tmp_path: Path):
    hunks = [hunk(path="README.md"), hunk(path="docs/x.rst"), hunk(path="logo.png")]
    assert levels(review.classify(tmp_path, hunks, now=NOW)) == [review.PROSE] * 3


def test_sibling_test_in_the_same_diff_suppresses_the_trailing_note(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW)], started=NOW - 10, ended=NOW)
    alone = review.classify(tmp_path, [hunk()], now=NOW)[0]
    assert "no test file changed alongside" in alone.reason
    with_test = review.classify(tmp_path, [hunk(), hunk(path="tests/test_src.py")], now=NOW)
    assert "no test file changed alongside" not in with_test[0].reason


def test_status_note_names_new_and_deleted_hunks(tmp_path: Path):
    hunks = [hunk(path="new.py", status="untracked"), hunk(path="gone.py", status="deleted")]
    findings = review.classify(tmp_path, hunks, now=NOW)
    assert {f.hunk.path: f.reason.split(" — ")[0] for f in findings} == {
        "new.py": "new, untracked", "gone.py": "deleted"}


def test_corrupt_and_junk_records_never_raise(tmp_path: Path):
    path = tmp_path / ".tycho" / "turns.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not json at all\n"
        '{"files": "not-a-list", "commands": 7}\n'
        '{"files": [{"path": "src.py", "ts": "yesterday"}], "commands": [null, 3]}\n'
        '[1,2,3]\n'
        '{"files": [{"path": "src.py", "ts": 1.0}], "commands": [{"outcome": "passed",'
        ' "runner": true}]}\n'  # no timestamps at all — unusable, must not crash
    )
    assert levels(review.classify(tmp_path, [hunk()], now=NOW)) == [review.UNEXERCISED]


# --- ranking and rendering ---------------------------------------------------


def test_worst_findings_lead_and_bigger_hunks_come_first(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW - 100), ("tests/test_src.py", NOW - 100)],
                 started=NOW - 110, ended=NOW - 90, commands=[("python x.py", False, "passed")])
    write_record(tmp_path, files=[("cold.py", NOW)], started=NOW - 10, ended=NOW)
    hunks = [
        gitstate.Hunk("README.md", 1, 2, 1, 0),
        gitstate.Hunk("tests/test_src.py", 1, 2, 1, 0),
        gitstate.Hunk("unknown.py", 1, 2, 1, 0),
        gitstate.Hunk("src.py", 1, 2, 1, 0),
        gitstate.Hunk("cold.py", 1, 2, 1, 0),
        gitstate.Hunk("cold.py", 40, 90, 50, 0),
    ]
    findings = review.classify(tmp_path, hunks, now=NOW)
    assert levels(findings) == [
        review.UNEXERCISED, review.UNEXERCISED,   # cold.py, biggest hunk first
        review.UNTESTED, review.UNRECORDED, review.TEST, review.PROSE,
    ]
    assert findings[0].hunk.ref == "cold.py:40-90"
    assert review.unexercised(findings) == 3


def test_render_is_scannable_and_refuses_to_overclaim(tmp_path: Path):
    write_record(tmp_path, files=[("src.py", NOW)], started=NOW - 10, ended=NOW)
    out = "\n".join(review.render(
        review.classify(tmp_path, [hunk(start=88, end=114)], now=NOW), "HEAD"))
    assert "src.py:88-114" in out               # clickable file:line-range
    assert "UNEXERCISED" in out
    assert "\033[" not in out                   # never colour — piping this is first-class
    assert "cannot say these lines executed" in out


def test_render_caps_the_detail_list(tmp_path: Path):
    write_record(tmp_path, files=[(f"f{i}.py", NOW) for i in range(40)],
                 started=NOW - 10, ended=NOW)
    hunks = [gitstate.Hunk(f"f{i}.py", 1, 2, 1, 0) for i in range(40)]
    out = review.render(review.classify(tmp_path, hunks, now=NOW), "HEAD")
    assert sum(1 for line in out if ".py:" in line) == review._MAX_DETAIL
    assert any("and 20 more" in line for line in out)


# --- end to end --------------------------------------------------------------


def test_review_on_a_real_repo(tmp_path: Path):
    repo = repo_with_commit(tmp_path)
    text = (repo / "src.py").read_text().splitlines()
    text[4] = "CHANGED"
    (repo / "src.py").write_text("\n".join(text) + "\n")
    (repo / "README.md").write_text("docs\n")
    out = "\n".join(review.review(repo))
    assert "src.py:5" in out
    assert "UNRECORDED" in out           # no turn record here at all — and it says so
    assert "README.md" in out            # untracked, and classified as prose


def test_review_reports_no_changes_and_non_repos(tmp_path: Path):
    repo = repo_with_commit(tmp_path)
    assert review.review(repo) == ["tycho: no changes against HEAD."]
    assert review.review(repo, "nosuchref") == [
        "tycho: can't diff against nosuchref — no such commit."]
    assert review.review(tmp_path / "nope") == [
        "tycho: not a git repository — nothing to review."]


def test_review_of_a_repo_with_no_commits_still_sees_untracked_files(tmp_path: Path):
    _git(tmp_path, "init")
    (tmp_path / "brand_new.py").write_text("x = 1\n")
    out = "\n".join(review.review(tmp_path))
    assert "untracked files only" in out
    assert "brand_new.py:1" in out


def test_review_never_credits_a_run_recorded_before_the_edit_end_to_end(tmp_path: Path):
    repo = repo_with_commit(tmp_path)
    (repo / "src.py").write_text("changed\n")
    write_record(repo, files=[("src.py", 10.0)], started=1.0, ended=20.0,
                 commands=[("pytest -q", True, "passed")])
    assert "exercised: a passing test run" in "\n".join(review.review(repo))
    # Same repo, one more turn: edited again, nothing run since.
    write_record(repo, files=[("src.py", 500.0)], started=490.0, ended=510.0)
    out = "\n".join(review.review(repo))
    assert "UNEXERCISED" in out and "no recorded command ran after it" in out


def test_review_ignores_everything_tycho_init_wrote(tmp_path: Path):
    """A fresh install must not open the user's first review with 21 hunks of our own files.

    `.claude/` (settings + 19 slash-command docs) and `.tycho.toml` are ours to install, not
    the user's code to review — measured at 21 of 24 hunks on a freshly-initialised repo.
    """
    from tycho import review as review_mod

    for path in (".tycho/turns.jsonl", ".claude/settings.json",
                 ".claude/commands/tycho-verify.md", ".tycho.toml"):
        assert review_mod._is_own_state(tmp_path, path), f"{path} should be filtered out"
    for path in ("src/app.py", ".github/workflows/ci.yml", "claude/notes.md",
                 "docs/.tycho.toml"):
        assert not review_mod._is_own_state(tmp_path, path), f"{path} is the user's"

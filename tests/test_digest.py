"""The turn digest and its selectivity (strategy §9.1/§11.1).

Two things are load-bearing and the tests are grouped by them:

1. **The ladder is the spine.** It must show all four rungs and must never tick one the record
   can't support — `record.stage_of` returns the highest *matching* rung, not a chain, so
   `artifact_changed` does not imply anything ran.
2. **Silence is the product.** Every signal has to fire when it should *and stay quiet when it
   shouldn't*, decay when it stops being news, and — the subtle one — none of that may touch
   the verdict relay, which is a different channel answering a different question.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tycho import digest, hook, record, state
from tycho.model import Stage, Verdict

# --- record builders ---------------------------------------------------------


def rec(
    verdict: str = Verdict.VERIFIED.name,
    stage: str = Stage.CLAIM_SUPPORTED.value,
    files: int = 1,
    checks=(),
    claims=(),
    commands=None,
    turn_id: str = "abc123",
) -> dict:
    """A record dict shaped exactly like `record.build` writes one."""
    return {
        "schema": 1,
        "id": turn_id,
        "session": "s",
        "harness": "claude",
        "model": None,
        "agent_version": None,
        "started_at": 1.0,
        "ended_at": 2.0,
        "verdict": verdict,
        "stage": stage,
        "checks": list(checks),
        "files": [{"path": f"src/f{i}.py", "kind": "edit", "ts": 1.0} for i in range(files)],
        "commands": [{"cmd": "pytest -q", "runner": True, "outcome": "passed"}]
        if commands is None
        else list(commands),
        "claims": list(claims),
    }


def check(name: str, status: str, evidence: str = "because") -> dict:
    return {"name": name, "status": status, "evidence": evidence}


# --- the acceptance ladder ---------------------------------------------------


@pytest.mark.parametrize("stage", [s.value for s in Stage])
def test_ladder_shows_all_four_rungs_at_every_stage(stage):
    """The unreached rungs are the point — a truncated ladder can't say what's missing."""
    line = digest.render(rec(stage=stage))
    for rung in Stage:
        assert rung.value in line


def test_ladder_ticks_up_to_the_reached_rung_and_dots_the_rest():
    line = digest._ladder(rec(stage=Stage.ARTIFACT_CHANGED.value))
    assert line == "✓ attempted  ✓ executed  ✓ artifact_changed  · claim_supported"


def test_ladder_ticks_every_rung_at_the_top():
    assert digest._ladder(rec()).count("✓") == 4


def test_ladder_never_claims_a_run_that_did_not_happen():
    """`stage_of` picks the highest matching rung, not a chain: a turn that wrote a file but
    ran nothing is `artifact_changed`. Ticking `executed` under it would have Tycho assert a
    test run that never happened — the exact class of claim it exists to disprove."""
    line = digest._ladder(rec(stage=Stage.ARTIFACT_CHANGED.value, commands=[]))
    assert line == "✓ attempted  · executed  ✓ artifact_changed  · claim_supported"


def test_ladder_never_claims_a_file_landed_when_none_is_recorded():
    line = digest._ladder(rec(stage=Stage.CLAIM_SUPPORTED.value, files=0))
    assert "· artifact_changed" in line and "✓ claim_supported" in line


def test_ladder_ticks_nothing_for_an_unknown_stage():
    """A record from an older schema renders the ladder empty, never a traceback."""
    line = digest._ladder(rec(stage="teleported"))
    assert "✓" not in line and line.count("·") == 4


# --- signals: each one fires ... ---------------------------------------------


def test_adverse_verdict_speaks_and_names_the_failing_check():
    signal = digest.speaks(rec(verdict="FAILED", checks=[check("test_freshness", "STALE", "old")]))
    assert signal is not None and signal.name == "adverse"
    assert "test_freshness" in signal.headline and "old" in signal.headline


@pytest.mark.parametrize("verdict", ["FAILED", "STALE", "OVERRIDDEN"])
def test_every_adverse_verdict_speaks(verdict):
    assert digest.speaks(rec(verdict=verdict)) is not None


def test_unbacked_claim_speaks_when_prose_says_done_but_the_ladder_stopped_short():
    """§4's 'code written, tests never ran' — and it fires on a turn the verdict is happy with,
    which is exactly the gap the verdict-shaped output left open."""
    signal = digest.speaks(
        rec(stage=Stage.ARTIFACT_CHANGED.value, claims=["Fixed the retry logic, all tests pass."])
    )
    assert signal is not None and signal.name == "unbacked_claim"
    assert "artifact_changed" in signal.headline


def test_regression_speaks_on_the_first_unproven_turn_after_a_green_run():
    history = [rec() for _ in range(4)]
    signal = digest.speaks(rec(verdict="INDETERMINATE"), history)
    assert signal is not None and signal.name == "regression"
    assert "4" in signal.headline


def test_blast_radius_speaks_when_a_turn_dwarfs_this_repos_recent_turns():
    history = [rec(files=1) for _ in range(6)]
    signal = digest.speaks(rec(files=9), history)
    assert signal is not None and signal.name == "blast_radius"
    assert "9 files" in signal.headline


# --- ... and each one stays quiet ---------------------------------------------


def test_a_routine_proven_turn_says_nothing():
    """The whole product: 'VERIFIED again, all nine checks pass' is not news (§11.1)."""
    assert digest.speaks(rec(), [rec() for _ in range(6)]) is None


def test_unbacked_claim_stays_quiet_when_the_ladder_actually_got_there():
    assert digest.speaks(rec(claims=["Fixed it — all tests pass."])) is None


def test_unbacked_claim_stays_quiet_on_work_in_progress_that_claims_nothing():
    """Stopping at `artifact_changed` mid-task is normal; interrupting for it is the wallpaper."""
    wip = rec(stage=Stage.ARTIFACT_CHANGED.value, claims=["Looking at the parser."])
    assert digest.speaks(wip) is None


def test_regression_stays_quiet_without_a_real_green_streak():
    """Two greens is a coincidence, not a streak."""
    assert digest.speaks(rec(verdict="INDETERMINATE"), [rec(), rec()]) is None


def test_regression_stays_quiet_when_the_repo_was_already_unproven():
    history = [rec(verdict="INDETERMINATE") for _ in range(5)]
    assert digest.speaks(rec(verdict="INDETERMINATE"), history) is None


def test_blast_radius_stays_quiet_on_a_repo_where_wide_turns_are_normal():
    """The point of reading this repo's own record: 9 files is unremarkable here."""
    history = [rec(files=8) for _ in range(6)]
    assert digest.speaks(rec(files=9), history) is None


def test_blast_radius_stays_quiet_below_the_floor():
    """3 files against a median of 1 is a multiple, not a blast radius."""
    assert digest.speaks(rec(files=3), [rec(files=1) for _ in range(6)]) is None


def test_blast_radius_stays_quiet_without_enough_history_to_have_a_norm():
    """A fresh repo has no norm to deviate from; inventing one is loudest when we know least."""
    assert digest.speaks(rec(files=30), [rec(files=1)]) is None


# --- novelty decay ------------------------------------------------------------


def test_the_same_signal_three_turns_running_stops_being_news():
    failing = [check("test_freshness", "STALE", "old")]
    prior = rec(verdict="FAILED", checks=failing)
    assert digest.speaks(prior, []) is not None                 # 1st: news
    assert digest.speaks(prior, [prior]) is not None             # 2nd: still news
    assert digest.speaks(prior, [prior, prior]) is None          # 3rd: not news any more


def test_decay_is_keyed_on_the_specific_condition_not_the_signal_kind():
    """Two FAILED turns on the same check are the same news; a FAILED turn on a check that was
    fine yesterday is new news, even though both are the `adverse` signal."""
    old = rec(verdict="FAILED", checks=[check("test_freshness", "STALE")])
    new = rec(verdict="FAILED", checks=[check("scope_drift", "FAIL")])
    assert digest.speaks(new, [old, old]) is not None


def test_a_signal_that_fired_once_two_turns_ago_is_still_news():
    """Intersection, not union — an intermittent condition never decays into silence."""
    bad = rec(verdict="FAILED", checks=[check("test_freshness", "STALE")])
    assert digest.speaks(bad, [rec(), bad]) is not None


def test_decay_can_be_switched_off_for_a_caller_who_asked_for_everything():
    bad = rec(verdict="FAILED", checks=[check("test_freshness", "STALE")])
    assert digest.speaks(bad, [bad, bad], decay=False) is not None


# --- rendering ----------------------------------------------------------------


def test_the_unprompted_digest_fits_the_hook_output_budget():
    """§6.1 says ~4 lines, and §4 says evidence nobody reads is evidence that doesn't exist."""
    for signal in (None, digest.Signal("x", "x", "y" * 200)):
        assert len(digest.brief(rec(files=40, claims=["a" * 500]), signal).splitlines()) <= 4


def test_the_brief_leads_with_why_we_spoke_not_with_the_verdict():
    signal = digest.speaks(rec(verdict="FAILED", checks=[check("scope_drift", "FAIL", "wat")]))
    first = digest.brief(rec(verdict="FAILED"), signal).splitlines()[0]
    assert "scope_drift" in first


def test_the_full_digest_is_a_receipt_of_the_turn():
    text = digest.render(
        rec(
            verdict="FAILED",
            stage=Stage.EXECUTED.value,
            files=2,
            checks=[check("file_state", "FAIL", "src/f0.py missing")],
            claims=["Added the helper."],
        )
    )
    assert "src/f0.py" in text          # what changed
    assert "pytest -q → passed" in text  # what ran, and what it returned
    assert "Added the helper." in text   # what was claimed
    assert "file_state — src/f0.py missing" in text  # what is still unverified
    assert "FAILED" in text.splitlines()[0]           # the verdict: one field, not the headline


def test_the_full_digest_says_so_when_nothing_is_outstanding():
    assert "nothing" in digest.render(rec()).splitlines()[-1]


# --- malformed records --------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"verdict": None, "stage": None, "files": None, "commands": None, "claims": None},
        {"files": "not a list", "checks": 7, "claims": {"a": 1}},
        {"files": [None, 5, {"path": None}], "commands": [{"cmd": None}], "claims": [None, 3]},
        {"verdict": ["FAILED"], "stage": 12, "id": 99},
        {"checks": [{"status": "FAIL"}]},  # a failing check with no name and no evidence
    ],
)
def test_a_malformed_record_renders_rather_than_raises(row):
    """Records come off disk — older schema, crashed append, hand-edited. Coerce, never raise."""
    assert isinstance(digest.render(row), str)
    assert isinstance(digest.brief(row, digest.speaks(row)), str)
    assert isinstance(digest.signals(row, [row, {}]), tuple)


def test_speaks_never_raises_on_junk_history():
    assert digest.speaks(rec(), [{}, {"verdict": None}, {"files": "x"}]) is None


# --- the hook: silence on routine turns, a digest on anomalous ones -----------
#
# Real Stop payloads through `hook.run`, because the seam being tested (relay vs digest) only
# exists there. The transcripts are synthesized rather than fixtures because the whole point is
# a *sequence* of turns in one repo — history is what the selectivity reads.

BASE = time.time() - 10_000.0


def _transcript(repo: Path, name: str, *, n: int, files, run=None, claim: str) -> Path:
    """One single-turn Claude transcript, with the edited files really written to disk.

    `os.utime` pins each file's mtime to its event timestamp so `test_freshness` sees the same
    ordering the transcript describes — otherwise every synthetic turn reads as STALE.
    """
    t = BASE + n * 100
    rows = [{"type": "system", "timestamp": _iso(t), "content": "start"}]
    for i, (path, content) in enumerate(files):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        os.utime(target, (t, t))
        rows += [
            {"type": "assistant", "timestamp": _iso(t), "message": {"role": "assistant",
             "content": [{"type": "tool_use", "id": f"e{n}{i}", "name": "Write",
                          "input": {"file_path": path, "content": content}}]}},
            {"type": "user", "timestamp": _iso(t + 1), "toolUseResult": {
                "type": "create", "filePath": path, "content": content, "originalFile": None,
                "structuredPatch": []},
             "message": {"role": "user", "content": [{"type": "tool_result",
              "tool_use_id": f"e{n}{i}", "content": "File created", "is_error": False}]}},
        ]
        t += 2
    if run:
        rows += [
            {"type": "assistant", "timestamp": _iso(t), "message": {"role": "assistant",
             "content": [{"type": "tool_use", "id": f"r{n}", "name": "Bash",
                          "input": {"command": run}}]}},
            {"type": "user", "timestamp": _iso(t + 1), "toolUseResult": {
                "stdout": "12 passed in 0.4s", "stderr": "", "interrupted": False,
                "isImage": False},
             "message": {"role": "user", "content": [{"type": "tool_result",
              "tool_use_id": f"r{n}", "content": "12 passed in 0.4s", "is_error": False}]}},
        ]
        t += 2
    rows.append({"type": "assistant", "timestamp": _iso(t), "message": {
        "role": "assistant", "content": [{"type": "text", "text": claim}]}})
    path = repo / ".t" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _iso(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _repo(tmp_path: Path) -> Path:
    """A repo Tycho recognizes as having tests — otherwise the test checks are all disabled."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    return tmp_path


def _routine(repo: Path, n: int) -> Path:
    """A proven turn: wrote a file, ran the suite after it, said so. The 90% case."""
    return _transcript(
        repo, f"routine{n}", n=n, files=[(f"src/m{n}.py", f"x = {n}\n")],
        run="pytest -q", claim="Added the helper; tests pass.",
    )


def _stop(repo: Path, transcript: Path) -> str:
    return json.dumps({"cwd": str(repo), "transcript_path": str(transcript)})


def test_hook_says_nothing_at_all_on_a_routine_turn(tmp_path: Path):
    repo = _repo(tmp_path)
    assert [hook.run(_stop(repo, _routine(repo, n))) for n in range(4)] == [None] * 4


def test_a_silent_turn_is_still_recorded(tmp_path: Path):
    """Silence is a decision not to interrupt, not a decision not to verify — `tycho show`
    and `tycho blame` must still have the turn."""
    repo = _repo(tmp_path)
    assert hook.run(_stop(repo, _routine(repo, 0))) is None
    assert len(record.read(repo)) == 1


def test_hook_speaks_with_a_tight_digest_on_an_anomalous_turn(tmp_path: Path):
    repo = _repo(tmp_path)
    for n in range(3):
        hook.run(_stop(repo, _routine(repo, n)))
    anomalous = _transcript(
        repo, "claimed", n=9, files=[("src/retry.py", "def retry():\n    return 1\n")],
        run=None, claim="Fixed the retry logic — all tests pass.",
    )
    out = hook.run(_stop(repo, anomalous))
    assert out is not None
    text = out["systemMessage"]
    assert "evidence stopped at" in text
    assert "· executed" in text  # and it does not claim the run that never happened
    assert len(text.splitlines()) <= 4


def test_hook_never_raises_on_a_record_it_cannot_digest(tmp_path: Path, monkeypatch):
    """The Stop hook's standing contract. A digest that blows up costs at most the digest."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(digest, "speaks", lambda *a, **k: 1 / 0)
    assert hook.run(_stop(repo, _routine(repo, 0))) is None  # routine turn: nothing was lost


def test_a_digest_failure_falls_back_to_the_verdict_on_an_unproven_turn(tmp_path: Path,
                                                                        monkeypatch):
    """Losing an adverse verdict to a rendering bug is the one outcome worse than noise."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(digest, "speaks", lambda *a, **k: 1 / 0)
    bad = _transcript(repo, "bad", n=1, files=[("src/x.py", "x = 1\n")], run=None,
                      claim="I searched the web for the docs.")
    out = hook.run(_stop(repo, bad))
    assert out is not None and "Tycho:" in out["systemMessage"]


# --- the seam: the relay is a different channel answering a different question --


def test_the_relay_still_fires_on_a_turn_whose_digest_stays_silent(tmp_path: Path):
    """The subtle one. "Should the agent fix this?" and "should we interrupt the human?" are
    different questions: the digest decays a standing failure into silence, the relay must keep
    pushing the agent at it regardless. A regression here is invisible until someone notices the
    agent quietly stopped fixing things."""
    repo = _repo(tmp_path)
    state.set_relay_enabled(repo, True)
    unproven = [
        _transcript(repo, f"u{n}", n=n, files=[(f"src/u{n}.py", "x = 1\n")], run=None,
                    claim="I searched the web for the docs.")
        for n in range(4)
    ]
    for t in unproven[:3]:
        hook.run(_stop(repo, t))
        state.reset_relay_streak(repo)  # a fresh user turn each time, so the leash isn't spent
    # By now the same condition has fired twice running — the human-facing digest has decayed.
    history = record.read(repo, limit=digest.HISTORY)
    turn = record.read(repo, limit=1)[0]
    assert digest.speaks(turn, history[1:]) is None

    out = hook.run(_stop(repo, unproven[3]))
    assert out is not None
    assert "hookSpecificOutput" in out  # ...and the relay fired anyway
    assert "additionalContext" in out["hookSpecificOutput"]


def test_the_digest_never_leaks_into_the_model_channel(tmp_path: Path):
    """`additionalContext` is the model's copy and must stay the adverse-check report: the
    digest is written for a human reading a terminal, and shipping it to the model both spends
    context and re-describes a turn the model just took."""
    repo = _repo(tmp_path)
    state.set_relay_enabled(repo, True)
    bad = _transcript(repo, "bad", n=1, files=[("src/x.py", "x = 1\n")], run=None,
                      claim="I searched the web for the docs.")
    context = hook.run(_stop(repo, bad))["hookSpecificOutput"]["additionalContext"]
    assert "`tycho show`" not in context
    assert "attempted" not in context  # no ladder — that is the human's view


def test_relay_semantics_are_untouched_by_selectivity(tmp_path: Path):
    """Relay off (the default) is still the only thing that keeps Tycho out of the model's
    context — silence on the human channel must not have quietly opened one."""
    repo = _repo(tmp_path)
    for n in range(4):
        out = hook.run(_stop(repo, _routine(repo, n)))
        assert out is None or set(out) == {"systemMessage"}


def test_an_unproven_turn_always_reaches_the_human_when_the_relay_is_on(tmp_path: Path):
    """Turning the relay on is an operator saying "keep going until this is VERIFIED", so the
    turn where the relay's leash runs out must not end in total silence on a standing failure."""
    repo = _repo(tmp_path)
    state.set_relay_enabled(repo, True)
    outs = [
        hook.run(_stop(repo, _transcript(
            repo, f"u{n}", n=n, files=[(f"src/u{n}.py", "x = 1\n")], run=None,
            claim="I searched the web for the docs.")))
        for n in range(6)
    ]
    assert all(o is not None for o in outs)          # never silent while the relay is on
    assert not all("hookSpecificOutput" in o for o in outs)  # and the leash really did run out

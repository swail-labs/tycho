"""Every harness in the registry, held to the same contract.

`test_eval.py` grades the *engine* over frozen Sessions. This file grades the *integration*:
detection, payload reading, parsing, output shape, and — the part nothing else covered — that
what a harness records and what Tycho claims to see are the same list.

**Why capabilities are asserted in both directions.** A parser returning nothing looks
identical to a harness that records nothing. One is a bug, the other is a fact about the
world, and until they were declared apart the suite could not tell them apart either: a reader
that broke on a Codex release would present as "codex records less now", indistinguishable
from health. So a declared capability must yield real data, *and* an undeclared one must yield
none. The second half is the one that keeps Tycho honest — it is where a fabricated verdict
would come from.

Everything here iterates `harness.ALL`, so adding a `Harness` to the registry auto-enrolls it
and missing assets fail as tests rather than being silently skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import harness_assets as assets
from tycho.read import events as events_mod
from tycho.read import harness as harness_mod

NAMES = [h.name for h in harness_mod.ALL]


@pytest.fixture(params=NAMES)
def name(request) -> str:
    return request.param


# --- detection --------------------------------------------------------------
#
# `detect` is ordered shape-sniffing over a payload dict, which means every harness added can
# shadow one already there — Codex's row is `hook_event_name == "Stop"` plus a `turn_id`, and
# a harness arriving with both would silently steal Codex's traffic. A row-only test never
# catches that; only the full matrix does.

def test_each_payload_routes_to_its_own_harness(name: str):
    assert harness_mod.detect(assets.payload(name)).name == name


@pytest.mark.parametrize("other", NAMES)
def test_no_payload_routes_to_a_harness_that_is_not_its_own(name: str, other: str):
    """The off-diagonal of the detect matrix — the half that catches shadowing."""
    if name == other:
        return
    routed = harness_mod.detect(assets.payload(name)).name
    assert routed != other, (
        f"{name}'s Stop payload routed to {other} — `detect` is ordered shape-sniffing and "
        f"one of those two rows now shadows the other"
    )


def test_claude_is_the_fallback_for_an_unrecognized_payload():
    """Pinned because it is load-bearing: an unknown payload must land somewhere, and the
    fallback being Claude is what makes every other row an explicit opt-out."""
    assert harness_mod.detect({}).name == "claude"


# --- payload contract -------------------------------------------------------

def test_repo_root_reads_the_payload(name: str):
    root = harness_mod.BY_NAME[name].repo_root(assets.payload(name))
    assert isinstance(root, Path)
    assert root != Path(), f"{name}: repo_root returned an empty path"


def test_transcript_of_reads_the_payload(name: str):
    """A file-backed harness hands us the path; OpenCode rebuilds from its DB instead.

    `transcript_is_file` is exactly that distinction, so this asserts against the declaration
    rather than special-casing a name — a harness that changes which side it is on has to move
    its capability, where the change is reviewable.
    """
    harness = harness_mod.BY_NAME[name]
    payload = assets.payload(name)
    if not harness.capabilities.transcript_is_file:
        # No OpenCode DB in the test environment, so the honest outcome is None — never a
        # raise, which the hook would swallow into silence.
        assert harness.transcript_of(payload) is None
        return
    assert harness.transcript_of(payload) == Path(payload["transcript_path"])


# --- output contract --------------------------------------------------------
#
# One renamed key here and verdicts stop reaching humans while the hook still exits 0 — the
# exact silent failure this file exists for.

_OUTPUT_KEYS = {
    "claude": "systemMessage",
    "codex": "systemMessage",
    "cursor": "followup_message",
    "opencode": "message",
}


def test_format_output_carries_the_verdict_under_the_documented_key(name: str):
    out = harness_mod.BY_NAME[name].format_output("Tycho: FAILED")
    assert list(out) == [_OUTPUT_KEYS[name]], f"{name}: unexpected output keys {list(out)}"
    assert "Tycho: FAILED" in out[_OUTPUT_KEYS[name]]


def test_notice_output_when_present_uses_the_same_channel(name: str):
    harness = harness_mod.BY_NAME[name]
    if harness.notice_output is None:
        # Cursor suppresses notices deliberately: its only output field is model-facing, and
        # a notice reaching the model could commission a self-update.
        assert name == "cursor"
        return
    out = harness.notice_output("Tycho is watching")
    assert list(out) == [_OUTPUT_KEYS[name]]
    assert "Tycho is watching" in out[_OUTPUT_KEYS[name]]


# --- golden parse -----------------------------------------------------------

def test_parse_matches_the_golden_projection(name: str, request):
    """The reader's output, pinned as a normalized projection (see `assets.project`).

    Regenerate with `pytest --update-goldens` and *read the diff* — that diff is the
    re-verification `VERIFIED_AGAINST` currently asks a human to do from memory.
    """
    actual = assets.project(assets.parsed(name))
    path = assets.golden_path(name)
    if request.config.getoption("--update-goldens"):
        path.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"rewrote {path.name}")
    assert path.exists(), f"no golden for {name} — run `pytest --update-goldens`"
    assert actual == json.loads(path.read_text(encoding="utf-8"))


def test_parse_finds_something(name: str):
    """Positive evidence, because Tycho's characteristic failure is the quiet no-op.

    A reader that silently returns () is indistinguishable from a clean turn everywhere else
    in the product. Here it is a failure.
    """
    assert assets.parsed(name), f"{name}: parsed its own corpus into zero events"


# --- capabilities, both directions ------------------------------------------

def test_declared_capabilities_are_backed_by_real_data(name: str):
    """Declaring a capability is a claim about the transcript; this collects on it."""
    harness = harness_mod.BY_NAME[name]
    caps = harness.capabilities
    path = assets.TRANSCRIPTS[name]
    evts = assets.parsed(name)

    if caps.records_timestamps:
        assert any(e.ts > 0 for e in evts), "declares timestamps, parsed none"
    if caps.records_exit_status:
        assert any(e.is_error is not None for e in evts), "declares exit status, parsed none"
    if caps.records_runner_output:
        assert any(e.result.get("stdout") or e.result.get("stderr") for e in evts), \
            "declares runner output, parsed none"
    if caps.records_prose:
        assert harness.messages(path), "declares prose, parsed none"
    if caps.records_attribution:
        attributed = harness.attribution(path)
        assert any((attributed.model, attributed.agent_version, attributed.session_id)), \
            "declares attribution, parsed none"
    if caps.has_turn_ids:
        assert harness.turn_start(path) > 0, "declares turn ids, resolved no boundary"


def test_undeclared_capabilities_degrade_instead_of_guessing(name: str):
    """The half that matters most: silence where there is no evidence.

    A harness that records no prose must yield no prose — not an empty-but-present value the
    checks then reason over, and above all not something inferred. This is the structural
    guard against a fabricated verdict; `test_eval.py` asserts that promise at the engine
    level, and this asserts it at the reader.
    """
    harness = harness_mod.BY_NAME[name]
    caps = harness.capabilities
    path = assets.TRANSCRIPTS[name]
    evts = assets.parsed(name)

    if not caps.records_timestamps:
        assert all(e.ts == 0 for e in evts), "records no timestamps but events carry one"
    if not caps.records_exit_status:
        assert all(e.is_error is None for e in evts), "records no status but events carry one"
    if not caps.records_runner_output:
        assert not any(e.result.get("stdout") or e.result.get("stderr") for e in evts), \
            "records no output but events carry some"
    if not caps.records_prose:
        assert not harness.messages(path), "records no prose but messages() returned some"
    if not caps.records_attribution:
        attributed = harness.attribution(path)
        assert not any((attributed.model, attributed.agent_version, attributed.session_id)), \
            "records no attribution but attribution() returned a value — a guess is worse " \
            "than a null, the decay ledger is only worth anything if it was observed"


def test_edit_originals_match_the_declaration(name: str):
    """`records_edit_originals` decides whether a diff needs git for its baseline, so a wrong
    declaration shows up as a silently empty diff rather than an error."""
    edits = events_mod.file_edits(assets.parsed(name))
    if not edits:
        return
    has_original = any(e.original is not None for e in edits)
    if harness_mod.BY_NAME[name].capabilities.records_edit_originals:
        assert has_original, "declares edit originals, parsed none"
    else:
        assert not has_original, "records no edit originals but one was parsed"


# --- robustness -------------------------------------------------------------
#
# Mutations of the *captured* fixture, not synthetic files: a fuzz corpus invented by whoever
# wrote the parser inherits their assumptions about the shape. Starting from real bytes and
# damaging them is the cheapest way out of that.

def _mutations(raw: bytes) -> dict[str, bytes]:
    return {
        "empty": b"",
        "truncated_final_line": raw[: max(1, len(raw) - 40)],
        "invalid_utf8": raw[:200] + b"\xff\xfe" + raw[200:],
        "blank_lines_throughout": raw.replace(b"\n", b"\n\n"),
        "one_huge_line": raw + b"\n" + b'{"x":"' + b"z" * 1_000_000 + b'"}',
        "not_json_at_all": b"this is not a transcript\n" * 50,
        "unknown_tool_names": raw.replace(b'"Bash"', b'"Teleport"').replace(b'"bash"', b'"teleport"'),
    }


@pytest.mark.parametrize("mutation", list(_mutations(b"{}")))
def test_a_damaged_transcript_never_raises(name: str, mutation: str, tmp_path: Path):
    """The reader may return nothing; it may never raise.

    The hook swallows exceptions and exits 0, so a raise here is not a crash the user sees —
    it is Tycho going quiet for that session forever while the bad byte sits there.
    """
    source = assets.TRANSCRIPTS[name]
    damaged = tmp_path / source.name
    damaged.write_bytes(_mutations(source.read_bytes())[mutation])

    harness = harness_mod.BY_NAME[name]
    for reader in (harness.parse, harness.messages, harness.attribution, harness.turn_start):
        try:
            reader(damaged)
        except Exception as exc:  # noqa: BLE001 — the point is that nothing escapes
            pytest.fail(f"{name}.{reader.__name__} raised on {mutation}: {exc!r}")


# --- registry completeness --------------------------------------------------
#
# The enforcement point for "definition of done for a new harness": you cannot add a Harness
# the eval does not know about, because these iterate the registry rather than a hand-kept list.

def test_every_registered_harness_has_a_corpus(name: str):
    assert name in assets.TRANSCRIPTS, f"{name} is in ALL with no transcript in TRANSCRIPTS"
    assert assets.TRANSCRIPTS[name].exists(), f"{name}: corpus file is missing"
    assert (assets.HARNESS_DIR / name / "stop_payload.json").exists(), \
        f"{name}: no captured Stop payload"


def test_every_registered_harness_has_a_version_pin(name: str):
    """Including the ones with nothing to probe — OpenCode ships no CLI version, and an
    explicit `probe: None` is a recorded decision where a missing key is an oversight."""
    assert name in harness_mod.VERIFIED_AGAINST, f"{name}: no VERIFIED_AGAINST entry"
    assert "probe" in harness_mod.VERIFIED_AGAINST[name], f"{name}: pin declares no probe"


def test_capture_metadata_names_the_version_it_came_from(name: str):
    meta = assets.capture(name)
    assert meta, f"{name}: no capture.json"
    assert meta.get("version_captured"), f"{name}: capture names no harness version"
    assert "captured_by" in meta, f"{name}: capture does not say whether it was authored"


def test_an_enabled_harness_has_a_captured_corpus():
    """Enabled means users depend on it, and an authored fixture only proves its author's
    beliefs. Every reader bug found so far came from a shape nobody would have invented — so
    entering `ENABLED_NAMES` requires a corpus a tool captured off the real binary.
    """
    authored = [
        n for n in harness_mod.ENABLED_NAMES if not assets.capture(n).get("captured_by")
    ]
    assert not authored, (
        f"enabled but running on an authored corpus: {authored} — "
        f"run `python scripts/capture_harness.py <name>` against a real session"
    )

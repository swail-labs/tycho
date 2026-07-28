"""The invariants Tycho defends, made mechanical rather than kept by discipline (§8).

These are not feature tests. They are the properties that make the product what it claims to
be, and each one is here because *discipline alone loses to gravity*: open-core drifts toward
connectors, verifiers drift toward asking a model, and both would be a one-line change that no
other test in this suite would notice.

1. **Free never leaves the machine.** The whole trust path — gather, check, verdict, record,
   digest, hook — must not open a socket. Today this is true by construction (the only network
   call in the tree is the PyPI update check in `version.py`, off the hot path and cache-only
   on the Stop path). This test is what makes it stay true.
2. **No LLM in the trust path, permanently.** Not a policy line in a README: nothing in the
   verifying modules may import an SDK or reach a model endpoint.

Both are headline claims in the README and both are load-bearing for the paid tiers — an
auditor that can cause an effect, or that a poisoned ticket can talk out of a verdict, is an
auditor whose attestations are worth nothing.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from tycho import checks as checks_mod
from tycho import digest, record, review
from tycho import verify as engine
from tycho.model import Verdict

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"

# Every module that can influence a verdict. `version` and `init` are deliberately absent:
# the update check is a real (opt-out, off-hot-path) network call, and neither can move a
# verdict. If a module is added to the trust path, add it here.
TRUST_PATH = (
    "tycho.verify", "tycho.checks", "tycho.astdiff", "tycho.events", "tycho.runlog",
    "tycho.model", "tycho.record", "tycho.report", "tycho.digest", "tycho.state",
    "tycho.gitstate", "tycho.fsstate", "tycho.config", "tycho.harness", "tycho.hook",
    "tycho.review", "tycho.archaeology", "tycho.attest", "tycho.command", "tycho.opencode",
)

# Import names that would mean a model is being consulted. Substring match, so
# `anthropic`, `openai`, `google.generativeai`, `litellm` etc. are all caught.
_LLM_MARKERS = (
    "anthropic", "openai", "litellm", "langchain", "transformers", "torch",
    "generativeai", "ollama", "cohere", "mistralai", "huggingface",
)


@pytest.fixture()
def no_network(monkeypatch):
    """Make any attempt to reach the network an immediate, loud failure.

    Patches `connect`/`getaddrinfo`, **not** the `socket` class itself: `ssl.SSLSocket`
    subclasses `socket.socket`, so replacing the class breaks the stdlib on import rather
    than catching anything. Constructing a socket is harmless anyway — *connecting* is the
    act this invariant is about, and `getaddrinfo` catches the resolve that precedes it.
    """
    def _forbidden(*a, **k):
        raise AssertionError(
            "the trust path reached the network — Tycho Free must never leave the machine (§8)"
        )

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    return _forbidden


def test_nothing_a_user_can_invoke_opens_a_socket(tmp_path, no_network):
    """gather -> checks -> verdict -> build -> append, then every surface that reads it back:
    digest / review / blame / log / attest / the decay ledger."""
    from tycho import archaeology, attest, cli, state

    session = engine.gather(FIXTURE, tmp_path)
    results = engine.run_checks(session)
    verdict = engine.verdict_of(results)
    assert isinstance(verdict, Verdict)
    assert results  # it really did run, rather than short-circuiting to nothing

    rec = record.build(session, results, verdict, "claude", 1.0)
    assert record.digest(rec).startswith("sha256:")
    record.append(tmp_path, rec)
    assert record.read(tmp_path)

    assert digest.render(rec)
    digest.speaks(rec, record.read(tmp_path))
    review.review(tmp_path)
    archaeology.log(tmp_path)
    archaeology.blame(tmp_path, "src/app.py")
    attest.trailer(tmp_path)
    assert state.ledger(tmp_path)["turns"] == 1
    assert cli._ledger_lines(state.ledger(tmp_path))


def test_the_stop_hook_opens_no_socket(tmp_path, no_network):
    """The hook is the hot path: it runs after every single turn."""
    from tycho import hook

    payload = json.dumps({
        "transcript_path": str(FIXTURE),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
    })
    hook.run(payload)  # never raises by contract, and must not reach the network


def test_no_trust_path_module_imports_an_llm_sdk():
    """The claim is architectural, so it is checked against the modules, not the docs."""
    import importlib
    import sys

    for name in TRUST_PATH:
        importlib.import_module(name)
    loaded = {m.lower() for m in sys.modules}
    for marker in _LLM_MARKERS:
        offenders = sorted(m for m in loaded if marker in m)
        assert not offenders, f"an LLM SDK reached the trust path: {offenders}"


def test_no_trust_path_source_mentions_a_model_endpoint():
    """A raw HTTP call to a model API would dodge the import check above."""
    root = Path(__file__).parent.parent / "tycho"
    endpoints = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis")
    for name in TRUST_PATH:
        src = (root / f"{name.split('.')[-1]}.py").read_text(encoding="utf-8").lower()
        for endpoint in endpoints:
            assert endpoint not in src, f"{name} references a model endpoint: {endpoint}"


def test_checks_are_pure_functions_of_the_session(tmp_path, no_network):
    """The seam that lets Pro/Enterprise swap the evidence source without touching a check.

    Same frozen Session in, same results out, twice — a check that reached out to git or the
    filesystem on its own would be free to disagree with itself between calls.
    """
    session = engine.gather(FIXTURE, tmp_path)
    first = [(r.name, r.status, r.evidence) for r in checks_mod.run_checks(session)]
    second = [(r.name, r.status, r.evidence) for r in checks_mod.run_checks(session)]
    assert first == second


def test_the_suite_never_reads_the_developers_real_claude_home():
    """`init.global_installed()` reads the user-level Claude config.

    Without the conftest override, this suite's result would depend on whether the developer
    running it happens to have done `tycho init --global` — tests that pass on CI and fail on
    one laptop, for a reason nothing points at. Pinned here because the override lives in
    conftest, where it is easy to delete without noticing what it was for.
    """
    from tycho import init as init_mod

    global_dir = init_mod.claude_dir(Path.cwd(), init_mod.GLOBAL).resolve()
    real = (Path.home() / ".claude").resolve()
    assert global_dir != real
    assert real not in global_dir.parents


def test_the_advisory_marker_stays_deleted():
    """It declared an unbuilt LLM lane and contradicted the headline claim from inside the
    packaging metadata (§8). Deleting it was the decision; this keeps it decided."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"advisory:' not in pyproject

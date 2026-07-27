# Contributing to Tycho

Thanks for helping out. Tycho is an offline, stdlib-only verifier (Python ≥ 3.11, zero
runtime dependencies) that proves an agent did what it claimed — from git, the filesystem,
exit codes, and the harness event stream.

## Setup

```sh
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"       # or: uv pip install -e ".[dev]"
```

## Before you push

- **Tests:** `python -m pytest -q`
- **Lint:** `ruff check tycho tests`

Both run in CI (Linux, Python 3.11 / 3.12 / 3.13) on every pull request.

## Design invariants (don't break these)

- **Never blocks.** The Stop hook (`hook.py`) always exits 0 and fails open (returns `None`)
  on any error — so does the `prepare-commit-msg` trailer hook, which can never fail a commit.
  Only the manual commands exit non-zero, so CI can gate on them: `tycho verify` (1 on FAILED,
  3 on STALE), `tycho review --exit-code` (6), `tycho attest --verify` (7). Exit codes are a
  public contract — see `cli.ExitCode`; don't renumber them.
- **No LLM, no network in the trust path.** Only code renders a verdict. There is no advisory
  LLM lane and no pytest marker for one; `tests/test_invariants.py` is what keeps this true.
- **Harness-agnostic engine.** Checks run on a frozen, normalized `Session` and never learn
  which harness produced it. All harness-specific code lives in `harness.py` (the adapter)
  plus one `parse_*` reader in `events.py`.
- **Immutable.** `Session` / `Event` / `FileEdit` are frozen; `verify.gather()` is the only
  inbound I/O — everything downstream is pure.

## Adding support for a new harness

One adapter + one reader + one fixture, no engine change:

1. Add an adapter in `harness.py` (detect, repo root, transcript location, output format).
2. Add a `parse_*` reader in `events.py`, pinned to a real fixture in `tests/fixtures/`.
3. That's it — the engine and the checks stay untouched.

Each adapter's docstring in `harness.py` records the contract it depends on, and
`harness.VERIFIED_AGAINST` pins the harness version that contract was last checked
against — `tycho doctor` warns when the installed harness has moved past it.

## Commits

- **No AI-agent attribution.** Do not add `Co-authored-by` trailers or list any agent
  (Claude, Codex, Copilot, …) as author or co-author, regardless of which tool produced the
  change.
- Keep commits focused, and write *why*, not just *what*.

## Pull requests

Open a PR against `main`. CI (tests + `ruff`) must be green before merge.

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

- **Tests:** `python -m pytest -q -m "not e2e"` (`-m "not e2e"` skips the packaging tests, which
  build the wheel and sdist and need an index; drop it to run everything)
- **Lint:** `ruff check tycho tests`

Both run in CI (Linux, Python 3.11 / 3.12 / 3.13) on every pull request.

**If you change that test command, update `[tests] standing` in `.tycho.toml` to match.** The
two are coupled, and the coupling is silent when it breaks. A filter narrows a run, so a green
carrying `-m "not e2e"` cannot normally stand in for a red that ran without it; declaring the
filter standing is what cancels it from both sides. Undeclared, every green this project runs
narrows relative to every red — and because a green that follows an unresolved red is
disqualified outright, the first failing run would pin Tycho's last-green anchor for the rest of
the session. Re-running the suite could never clear it, and the verdict would report a constant
staleness that no action fixes. Declare only what genuinely runs every time: anything else in
that array hides a real narrowing.

## Repository layout

`tycho/` is layered, and the layers are the design claim rather than a filing system. Reading
bottom-up:

| | |
|---|---|
| `model.py` | the frozen types everything speaks — `Session`, `Event`, `CheckResult` |
| `engine/` | **pure.** The checks, the verdict reduction, the AST diff, the runner-output reader. Imports `model` and nothing else |
| `store/` | `<repo>/.tycho/` — install record, turn record, exec log, config. Every writer here is a concurrent writer |
| `read/` | the outside world: transcripts, git, the filesystem. `session.gather()` is the boundary |
| `views/` | recorded state → text a person reads |
| `wire/` | writes into other tools: harness hooks, commit trailer, status bar, `doctor`, the update check |
| `cli/` | the argparse surface. Nothing imports it |

The arrows only point down. `engine/` sits at the bottom because that is what "the checks are
pure functions over a frozen `Session`" means when you write it as directories: a check cannot
reach git or the network, because the package holding it cannot import the packages that can.
`test_the_engine_imports_nothing_that_can_do_io` enforces exactly that — without it the
directory names would be a suggestion.

If you find yourself wanting `from ..store import state` inside `engine/`, the thing you need
belongs on the `Session` instead. That is the seam, and it is the one that lets the evidence
source be swapped without touching a check.

| | |
|---|---|
| `tests/` | one file per surface; `test_eval.py` is the catch-rate/false-alarm harness, `test_invariants.py` the properties that must not drift |
| `packaging/` | the npm wrapper and Homebrew formula |
| `scripts/` | contributor helpers. `tycho-target.ps1.example` flips this repo's own hooks between `.venv` and `.venv-release` — copy it to `scripts/tycho-target.ps1` (gitignored) |
| `assets/` | logos |

## Design invariants (don't break these)

- **Never blocks.** The Stop hook (`wire/hook.py`) always exits 0 and fails open (returns `None`)
  on any error — so does the `prepare-commit-msg` trailer hook, which can never fail a commit.
  Only the manual commands exit non-zero, so CI can gate on them: `tycho verify` (1 on FAILED,
  3 on STALE), `tycho review --exit-code` (6), `tycho attest --verify` (7). Exit codes are a
  public contract — see `cli.ExitCode`; don't renumber them.
- **No LLM, no network in the trust path.** Only code renders a verdict. There is no advisory
  LLM lane and no pytest marker for one; `tests/test_invariants.py` is what keeps this true.
- **Harness-agnostic engine.** Checks run on a frozen, normalized `Session` and never learn
  which harness produced it. All harness-specific code lives in `read/harness.py` (the
  adapter) plus one `parse_*` reader in `read/events.py`.
- **No `harness.name` branches outside the registry.** Behaviour that varies by harness reads a
  declared `Capabilities` or `Channels` field instead. A name check is invisible to the next
  harness added, which is how one of them ended up verifying correctly and telling nobody;
  a declaration is a required constructor argument, so it gets answered.
- **Immutable.** `Session` / `Event` / `FileEdit` are frozen; `read.session.gather()` is the
  only inbound I/O — everything downstream is pure.

## Adding support for a new harness

One adapter + one reader + one captured corpus, no engine change. Run the eval as you go — it
is the checklist, and it tells you what is still missing:

```
python scripts/harness_eval.py
```

```
tycho harness eval
  harness     conform   reach   catch  corpus     pin
  claude        27 ok    7/8     1/1  captured   2.1.220 ok
  cursor        25 ok    1/8     0/1  authored   2026.07.09-a3815c0 ok  (not enabled)
```

The steps, each enforced by a test in `tests/test_harness_conformance.py`:

1. Add an adapter in `read/harness.py` (detect, repo root, transcript location) plus two
   required declarations, so neither can be skipped:
   - `Capabilities` — the honest list of what the harness *records*.
   - `Channels` — which audiences it can *reach*: a field only the human reads, a field only
     the model reads, or one shared by both. This is the one people get wrong. Codex shipped a
     release verifying every turn correctly and telling nobody, because its human-facing field
     is accepted and rendered nowhere, and the code assumed Claude's shape. **Confirm where
     your output actually lands before declaring it** — send a unique string through each field
     and go look.
2. Write `compose(human, model)` — the only per-harness output code. The hook decides *what*
   is said and to whom; `compose` decides how it is spelled on that harness's wire, and returns
   None for silence. Nothing else in the engine or the hook learns the new name: `_speak` and
   the relay read the declarations, so a harness with only a shared channel automatically gets
   the short spellings, the show-and-stop instruction, and no update notice.
3. Add a `parse_*` reader in `read/events.py`.
4. Add a Stop payload at `tests/fixtures/harness/<name>/stop_payload.json`, and name the
   transcript in `tests/harness_assets.py`. Capture that payload too — an authored one is
   missing whatever its author didn't know about.
5. Capture a real session — run the harness in a scratch repo, then
   `python scripts/capture_harness.py <name> --repo /path/to/scratch`. Authored fixtures only
   prove what their author already believed; every reader bug found so far was a shape nobody
   would have invented. Entering `ENABLED_NAMES` requires a captured corpus.
6. `pytest tests/test_harness_conformance.py --update-goldens`, then **read the diff** — that
   diff is the re-verification, and it is how a version bump is done from then on.
7. Add a `VERIFIED_AGAINST` entry (`probe: None` is fine if the harness ships no version).
8. `python scripts/harness_eval.py --update` to pin the initial floors.

`reach` is how much the harness records, not how well Tycho works: Cursor's 1/8 is a fact
about Cursor, and no amount of work here moves it. It stays printed rather than hidden,
because a harness that records nothing must *decline* — never fabricate a green — and the
declaration is what lets the suite tell "correctly blind" apart from "quietly broken".

Each adapter's docstring in `read/harness.py` records the contract it depends on, and
`harness.VERIFIED_AGAINST` pins the harness version that contract was last checked
against — `tycho doctor` warns when the installed harness has moved past it.

## Commits

- **No AI-agent attribution.** Do not add `Co-authored-by` trailers or list any agent
  (Claude, Codex, Copilot, …) as author or co-author, regardless of which tool produced the
  change.
- Keep commits focused, and write *why*, not just *what*.

## Pull requests

Open a PR against `main`. CI (tests + `ruff`) must be green before merge.

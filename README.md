<div align="center">

<img src="assets/swail-logo-rounded.png" alt="Swail" width="96">

# Tycho

**by [Swail](https://swail.dev)**

Never accept the model's summary as evidence.

[![CI](https://github.com/swail-labs/tycho/actions/workflows/ci.yml/badge.svg)](https://github.com/swail-labs/tycho/actions)
[![PyPI](https://img.shields.io/pypi/v/tycho-cli)](https://pypi.org/project/tycho-cli/)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Deps](https://img.shields.io/badge/deps-stdlib%20only-orange)](pyproject.toml)

</div>

---

A user on r/ClaudeAI put it better than we had:

> *"never accept the model's summary as evidence, only artifacts it can't fake by narrating"*

That is the whole product. Tycho is a local verifier that runs as a completion hook on your
agent's harness. When a turn ends it reads the artifacts — git, the filesystem, process exit
codes, and the harness's own event stream — and tells you what the turn can actually prove.

**Only a hook on the event stream sees the *process*.** A diff sees a result. CI sees a result.
A second model sees a diff. Tycho is the thing that was there when the agent ran `pytest`, got
exit 1, and then said tests pass — and it is the only one of those that can say so.

- **Instant, free, silent, always-on.** No API key, no second session, no tokens, no context
  burned. It runs on every turn and stays quiet unless something is off.
- **Nothing about your code leaves the machine.** No account, no LLM anywhere in the trust
  path. Verification is local computation over git, the filesystem, exit codes and the event
  stream — that path never opens a socket. The one thing that does: `tycho init` also installs
  a SessionStart hook that asks `pypi.org` once a day whether a newer Tycho exists, which
  discloses your Tycho version and your IP and nothing else. Turn it off with
  `TYCHO_NO_UPDATE_CHECK=1`.
- **Stdlib only**, Python ≥ 3.11. Nothing to audit, trivial install.
- **Claude Code** supported (auto-detected); other harnesses in development.
- **Linux, macOS, and Windows** — see [Platforms](#platforms).

**Keep your reviewer.** A second model reviewing the diff is a fine thing to do — it is also
slow, costs tokens, burns context, and can't prove it actually checked either. Tycho isn't a
replacement for it; it's the floor underneath it, running on every turn whether or not you also
ask a model.

## Platforms

| OS | Status | Verified by |
|----|--------|-------------|
| Linux | Supported | CI: `ubuntu-latest`, Python 3.11 / 3.12 / 3.13 — full suite, `ruff`, and a wheel + sdist install into a clean venv |
| Windows | Supported | CI: `windows-latest`, Python 3.12 — same suite, plus the `.exe` console-script hook under cp1252/backslash |
| macOS | Supported | Development baseline; not gated in CI |

**WSL2 is Linux** (a real Linux kernel), so it's covered by the Linux row. Install and run Tycho
*inside* the distro — launch your agent from the WSL shell — rather than driving `wsl.exe` from a
Windows-hosted agent, so Tycho's hook and the commands it verifies share one environment.

## Install

Fastest — the standalone binary, no Python needed:

```sh
curl -fsSL https://swail.dev/tycho/install.sh | sh
```

It downloads the binary for your platform, verifies it against the release checksums, and drops
the `tycho` command in `~/.local/bin`.

On macOS or Linux with Homebrew, the tap installs that same binary and keeps it upgradable
alongside the rest of your kegs:

```sh
brew install swail-labs/tap/tycho
```

**Have Python?** The PyPI package is **`tycho-cli`** (the name `tycho` is taken by an unrelated
project); it installs the **`tycho`** command:

```sh
uv tool install tycho-cli     # uv (recommended)
pipx install tycho-cli        # or pipx
```

Both drop the `tycho` command on your PATH in its own isolated environment. Other paths:

```sh
uvx --from tycho-cli tycho <command>   # run once, no install (uv)
uv add tycho-cli                       # add to a project's dependencies
pip install --user tycho-cli           # plain pip
```

Prefer an isolated *tool* install (`uv tool install` / `pipx`) over a bare `pip install`, which
drops the CLI into whatever environment you happen to be in. There's also an npm wrapper that
fetches the same checksummed binary:

```sh
npx @swail-labs/tycho <command>     # run once, cached after first use
npm install -g @swail-labs/tycho    # or put the `tycho` command on your PATH
```

Pick one channel per machine. `npm install -g` and `brew install` both want to own
`<brew-prefix>/bin/tycho` when your node is Homebrew-installed, so the second one you run won't
link. They ship the same binary from the same release — `brew` tells you how to choose if you hit
it.

## Quickstart

```sh
tycho init              # wire the hook into the harnesses you actually have
                        # (asks per harness; idempotent, self-healing, repo-local)
tycho init --yes        # skip the prompts (scripts and CI)
```

`tycho init` only touches Claude Code, and only inside this repo. It merges with hooks you already
have, backs up anything it changes, and refuses to touch a config it can't parse rather than risk
it. In one run it wires up:

- the **completion hook** that produces the turn digest,
- a **status-bar badge** and the `/tycho` slash commands (see [In Claude Code](#in-claude-code)),
- a `.tycho/` entry in your **`.gitignore`** — Tycho's state is machine-local and carries the
  agent's own prose; it is not something to put in someone else's PR,
- a git **`prepare-commit-msg`** hook that stamps the [`Tycho-Attestation:` trailer](#the-commit-trailer)
  onto commits an agent worked on. It is fenced inside any hook you already have, and it fails
  open by construction — it can neither block nor fail a commit.

Want it in every repo without running `init` in each one:

```sh
tycho init --global     # wire the user-level Claude config; defers to any per-repo install
```

The global install stays quiet outside git repos and steps aside wherever a repo-local install
exists, so the two never double-fire. The commit trailer stays per-repo — run `tycho init` inside
a repo to get it. Undo with `tycho uninstall --global`.

That's it. The next time an agent finishes a turn, Tycho verifies it — and most of the time says
nothing.

## What you see after a turn

Tycho used to print a verdict plus nine check lines after every single turn. It said `VERIFIED` so
often that nobody read it, which makes it worse than useless: the one turn that mattered looked
exactly like the two hundred that didn't.

Now the hook prints a **turn digest** — a receipt of what the turn actually did — and it is **rare
by design**. On a routine turn it prints nothing at all.

It speaks when one of four things is true, and the thresholds are relative to *your repo's*
history (from `.tycho/turns.jsonl`), not fixed numbers:

| Signal | Fires when |
|---|---|
| `adverse` | a check says the turn isn't proven — the headline names the check, not the verdict |
| `unbacked_claim` | the prose says done, but the evidence never reached `claim_supported` |
| `regression` | a run of proven turns just broke |
| `blast_radius` | far more files than this repo's recent turns touch (median-based, with a floor) |

Plus **novelty decay**: a condition that fired on the last two turns stops being news and goes
quiet. The same standing failure does not get shouted at you every turn — `tycho show` is always
there, and the [relay](#let-the-agent-see-its-own-verdict--the-relay), if you turn it on, keeps
pushing the agent regardless.

Four lines, anomaly first:

```
🔍 Tycho: FAILED — file_state: .github/workflows/review.yml — missing; docs/product-direction.md — missing
   ✓ attempted  · executed  ✓ artifact_changed  ✓ claim_supported
   2 files · ls -a && echo "=== workflows ===" && ls… passed · git remote -v && echo "=== CLAUDE.md? =… failed · +7 more
   `tycho show` for the full receipt
```

The second line is the **acceptance ladder** — see [the verdict lattice](#the-verdict-lattice)
below. Here it says the turn changed files and made a claim, but nothing ever *executed*.

This is not a report card for our checks. It's a receipt for the turn, and the checks are its
contents.

## The commands

Everything below runs as a plain `tycho <command>`. Grouped by the question you're asking:

### What just happened?

```sh
tycho show              # the full digest of the most recent turn
tycho show 5bf74f88     # or a specific turn id from `tycho log`
```

```
🔍 Tycho: turn f560db48130c3cfc · VERIFIED
   ladder   ✓ attempted  ✓ executed  · artifact_changed  ✓ claim_supported
   ran      pytest -q → passed
   claimed  "Dotfile blame works through the fixed root."
   open     nothing — every check that applied passed
```

`tycho verify` is the on-demand path that re-runs the whole engine over a session, rather than
reading the record:

```sh
tycho verify                     # auto-discovers the most-recently-used session
tycho verify --harness claude    # force a harness (default: whichever ran most recently)
tycho verify --session <path>    # verify a specific transcript
tycho verify --since HEAD~1      # diff from a git ref instead of HEAD
tycho verify --claim "added rate limiting, tests pass"   # echo the claim above the verdict
```

```
🔍 Tycho: FAILED
  ✗ test_freshness — auth_test.py passed 14:22:01, auth.py edited 14:22:47 (46s later)
  ✗ assertion_weakening — 2 assertions removed from test_login_expiry
  ✓ command_execution — pytest ran, exit 0
  • scope_drift — no [scope] set — run `tycho scope add '<glob>'` to bound edits (zero-config)
```

`tycho verify` exits non-zero on an adverse finding so you can gate in `pre-push`/CI. The hook
never blocks — it annotates; you decide.

### What did it do here before?

```sh
tycho log                        # the recorded history, newest first
tycho log -n 50                  # more of it
tycho log --verdict FAILED       # only failures
tycho log --since 2026-07-01     # only turns on or after this date
```

```
6m ago   f560db48  VERIFIED  claim_supported  no files  "Good instinct — dogfooding found a real b…"
35m ago  5bf74f88  VERIFIED  claim_supported  27 files  "Now the `.gitignore` gap and the verify/r…"
1h ago   4fb60e9b  VERIFIED  claim_supported  3 files   "Dotfile blame works through the fixed roo…"
1h ago   e7f58593  FAILED    claim_supported  2 files   "Two files added:"
```

The filters run *inside* the bounded stream, so `--verdict FAILED -n 20` gives you twenty
failures — not the failures among the last twenty turns.

```sh
tycho blame tycho/cli.py         # which turns touched this file
tycho blame tycho/cli.py:42      # a line works too — see the note it prints
```

```
tycho/cli.py — 1 turn, newest first
  note: asked for :42 — attribution is file-level. Tycho records which turns touched a file, not which lines.
  35m ago  VERIFIED  claim_supported  turn 5bf74f88  claude-opus-5
    "Now the `.gitignore` gap and the verify/record inconsistency. First, what does…" — no test ran
```

Git tells you what changed. `blame` tells you what the agent *said* it was doing and what backed
it — the `— no test ran` clause is the part only Tycho can say.

**It does not do line-level attribution, and it says so.** The record stores which turns touched a
*file*, never which lines. Joining lines to turns through `git blame` was rejected: it attributes
to a commit, and commits don't map onto turns (one commit is many turns, an uncommitted turn is no
commit, and a repo need not be git at all). That would put a confident wrong name on a line.

```sh
tycho count                      # how many problems Tycho has caught, here and all-time
tycho count --ledger             # per-model, per-check catch and blind rates
```

```
this repo: 25 runs, 0 blind (0%), 1 caught (1 FAILED) · all-time: 34 runs, 3 blind (9%), 4 caught (4 FAILED)

ledger: 4 turns on the record, 2026-07-27 → 2026-07-27, 0 blind (0%), 1 caught (25%)
  (the retained turn record — `count` above is the all-time tally)

  model                            turns  caught     blind
  claude-opus-5 (claude 2.1.220)       4  1 (25%)    0 (0%)

  check                    spoke  caught     blind      by model (caught/spoke)
  file_state                   3  1 (33%)    1 (25%)    claude-opus-5 1/3
  git_state                    3  1 (33%)    1 (25%)    claude-opus-5 1/3
  scope_drift                  3  0 (0%)     1 (25%)    claude-opus-5 0/3
  test_freshness               0  0 (—)      4 (100%)   claude-opus-5 0/0

  catch rate = caught / turns the check could speak to (PASS|FAIL|STALE).
  blind rate = blind / every turn it ran in — the metric that doesn't improve with model capability.
```

The **blind rate** is the number to watch, and it leads even at 0%. Catch rate falls as models get
better — that's models improving, not Tycho decaying. Blind rate is how often Tycho *couldn't
speak*, and nothing about a better model fixes it.

### What should I look at?

```sh
tycho review                     # against HEAD
tycho review --since HEAD~3      # against a ref
tycho review --exit-code         # exit 6 if a recorded change had nothing run after it
```

```
tycho review — 41 hunk(s) in 6 file(s) changed against HEAD

  ⚠ NO TEST RUN — a command ran after these, but no test runner did
      tycho/archaeology.py:1-23   edited 1h ago; a command ran after it, but no test runner
      tycho/review.py:1-21        edited 50m ago; a command ran after it, but no test runner
      tycho/record.py:230-242     edited 1h ago; a command ran after it, but no test runner

  ✓ 12 hunk(s) exercised: a passing test run recorded after the last edit (4 files)
  • 6 hunk(s) prose or assets, which no test run covers (README.md)

  "Exercised" means a command Tycho recorded ran after the hunk was written.
  Tycho has no per-line coverage — it cannot say these lines executed.
```

Hunks, not files, because `tycho/state.py:88-114` is somewhere to put your eyes. Ranked worst
first: `UNEXERCISED`, then `NO TEST RUN`, then `UNRECORDED`, then `TEST CHANGED` (a changed test
can't vouch for itself), then everything exercised or prose as one summary line.

**What it proves, exactly:** *"no command Tycho recorded ran after this hunk was written."* It
cannot say "this line never executed" — there is no per-line coverage here, and a command Tycho
never saw (a manual `pytest`, CI, a debugger) proves nothing to it either way. Every finding is
worded as what was *recorded*, because a review tool that overclaims coverage is worse than none.

`review` is advisory and exits `0` regardless of what it finds, unless you opt in with
`--exit-code`. That gate fires on `UNEXERCISED`/`NO TEST RUN` only — never on `UNRECORDED`, which
just means no turn Tycho saw touched the file (hand-written, or predating the install). Gating on
that would fail every honest commit.

### Make this run count

```sh
tycho exec -- pytest -q          # run it and put the real exit status on the record
tycho run  -- make test          # run it so the exit code is visible, without recording
```

`tycho exec` runs the command with Tycho's own stdio — your TTY, your colours, your streaming
output, your prompts — waits for it as the parent process, and writes what `wait()` returned to
`.tycho/commands.jsonl`. The exit code you see is **unchanged**, so it is safe to prefix onto
anything. A signal-killed child reports the conventional 128+signal; a command that doesn't exist
reports 127, and that too is evidence — "the claimed command does not exist" appears in no
transcript.

This is the reliable way to make any run visible, whatever it's wrapped in. Use it when:

- **Your runner is wrapped in a way Tycho doesn't recognize.** See
  [the `uv run --with` blind spot](#known-blind-spot-uv-run---with) below — this is currently a
  large hole, and `tycho exec` is the fix that works today.
- **The runner's name isn't in the command at all** — a Makefile target, a shell alias, a
  `./run-tests.sh` that calls pytest inside.
- **The shell masks the status** — `pytest; echo done`, `pytest | tail`.

Tycho already reads *through* a pipe (`pytest | tail`), a variable interpreter (`"$PY" -m
pytest`), and nested shells (`wsl.exe -d Ubuntu -- bash -c 'pytest'`, `ssh host 'pytest'`) on its
own. **WSL runs verify on their own; you don't need to wrap them.**

`tycho run` is the same shape without the evidence line — it makes the exit code visible to the
checks, but writes nothing to the command log. When in doubt, use `exec`.

### The commit trailer

```sh
tycho attest                     # print the trailer for what's staged
tycho attest --verify            # check HEAD's trailer against the record
tycho attest --verify <ref>      # or any commit
```

`tycho init` installs a `prepare-commit-msg` hook that stamps this onto commits an agent worked
on:

```
Tycho-Attestation: sha256:6f… (4 turns, 3 VERIFIED, 1 STALE)
Tycho-Attestation: sha256:1c… (2 turns, NEVER VERIFIED: 2 UNSUPPORTED)
```

It covers every recorded turn that touched a file in the commit, bounded by the commit's own
timestamp. A commit no recorded turn touched gets no trailer at all. `git log --grep 'NEVER
VERIFIED'` is the six-months-later query.

**What the trailer proves — and what it does not.** The digest is an unkeyed SHA-256 over a
record this machine wrote. There is no signature and no key, so it proves *consistency*, not
authenticity: this commit's trailer matches this machine's record as that record stands now.
That catches a trailer copied from one commit to another, a record edited after the fact, an
amend that changed the file set, and honest drift between what was verified and what was
committed — i.e. accidents and mistakes, which is what the trailer is for. It is not a defence
against a motivated agent or user: anyone who can write `.tycho/turns.jsonl` can write a record
claiming VERIFIED and produce a trailer that verifies clean against it. Read it as
tamper-evidence, never as proof that the work was verified.

**`--verify` has three answers, not two.** It recomputes the body from the record and compares:

```
e0d2832: attestation VERIFIED against the record — 4 turns, 3 VERIFIED, 1 STALE
e0d2832: attestation does NOT match the record — commit claims sha256:6f2a1c…, the record gives sha256:1c9f04… (4 turns, …)
e0d2832: claims sha256:6f2a1c… but the turn record no longer covers this commit (pruned, or .tycho/ is gone) — cannot confirm
e0d2832: no Tycho attestation — not agent-written, or Tycho wasn't installed
```

Only the second is a mismatch, and only it exits `7`. The last two are *cannot tell*, and they
exit `0` — a pruned record must never read as a forged one.

The stamping hook **can never fail your commit**. Its output is discarded, its non-zero exit is
swallowed even under `set -e`, and it never calls `exit`, so a hook of your own that it sits
inside still runs every line.

### The full command table

Matches `tycho help` and `tycho -h`:

| Command | Does |
|---|---|
| `verify` | verify what the agent claimed and render a verdict |
| `hook` | Stop-hook entrypoint: read hook JSON on stdin, verify, print |
| `init` | install Tycho's hook into this repo's detected harnesses |
| `doctor` | check that Tycho's hooks are installed, current, and firing |
| `uninstall` | remove Tycho's hooks (leaves your other hooks alone) |
| `statusline` | one line for a harness status bar: is Tycho live here, and the last verdict |
| `count` | how many problems Tycho has caught — in this repo, and all-time |
| `show` | the full digest of a turn: what changed, what ran, what's still unverified |
| `blame` | which turn touched this file, what the agent claimed, and what backed it |
| `log` | the recorded history of what agents did in this repo, newest first |
| `review` | risk-focus the diff: which changes no test covered and no command exercised |
| `attest` | print the `Tycho-Attestation` trailer for the latest recorded turn |
| `run` | run a command so its true exit code is seen even when wrapped/piped |
| `exec` | run a command and put its real output and exit status on the record |
| `scope` | show or edit which files the agent may edit (the `scope_drift` allowlist) |
| `relay` | let the agent see its verdict and keep working until VERIFIED (bounded, off by default) |
| `override` | record a per-check verdict override when the relay is on (agent-authorized, logged, off by default) |
| `update` | check for and install a newer Tycho |
| `help` | what Tycho is, whether it's live here, and every command |

### Exit codes

| Exit | Meaning | From |
|---|---|---|
| `0` | `VERIFIED` / `UNSUPPORTED` / `INDETERMINATE` — nothing adverse found | any |
| `1` | `FAILED` — a check proved the claim wrong | `verify` |
| `2` | usage error | any |
| `3` | `STALE` — edits landed after the last passing test run | `verify` |
| `4` | Tycho could not complete (unreadable transcript/config), or `init` refused a file | `verify`, `init` |
| `5` | installed here but not working | `doctor` |
| `6` | `UNEXERCISED` — a recorded change had no command run after it | `review --exit-code` |
| `7` | `MISMATCH` — the commit trailer does not match the record | `attest --verify` |

Each adverse kind gets its own code so a gate can pick what blocks: gate on `1` alone, or on `1`
and `3`, or add `6` if you want unexercised changes to stop a push. `6` is never `1` on purpose —
a hunk nothing exercised is a coverage claim, not proof anything is wrong.

## The verdict lattice

Binary done/not-done is the wrong shape, because the most common reality is neither: *code
written, tests never ran*. Tycho has six answers and an acceptance ladder, and the two
least-obvious ones do the most work.

| Verdict | Means |
|---|---|
| `VERIFIED` | at least one substantive check passed and nothing was adverse |
| `FAILED` | a check proved the claim wrong |
| `STALE` | there **is** evidence, and it predates the code it's supposed to cover |
| `INDETERMINATE` | checks ran; none of them could conclude |
| `UNSUPPORTED` | nothing here could speak to this turn at all |
| `OVERRIDDEN` | every adverse check was set aside by the agent, with a logged reason — see [override](#let-the-agent-see-its-own-verdict--the-relay) |

**`STALE` is the answer to "what about a test run from before the last edit?"** That is not a
pass and it is not a failure. `auth_test.py` passed at 14:22:01 and `auth.py` was edited at
14:22:47 — the green run is real, and it is 46 seconds too early to mean anything. It gets its own
verdict and its own exit code so you can decide whether it blocks.

**`INDETERMINATE` is the answer to "the code exists but nothing ran."** Tycho will not call that
verified and it will not call it failed. It says it couldn't tell, and — because "couldn't tell"
is useless without saying *how far it got* — every turn also carries an **acceptance ladder**:

```
✓ attempted  ✓ executed  · artifact_changed  ✓ claim_supported
```

| Rung | Reached when |
|---|---|
| `attempted` | the turn did something |
| `executed` | a recognized runner actually ran |
| `artifact_changed` | files on disk changed |
| `claim_supported` | the claim is backed by the evidence above |

The unreached rungs are the point, and they render as `·` rather than `✗` — a gap in the evidence
is not a failure. The two independently confirmable rungs (`executed`, `artifact_changed`) are
re-derived before being ticked, so a turn that wrote a file but ran nothing can never show a tick
under `executed`.

## The checks

`command_execution` · `test_freshness` · `test_provenance` · `assertion_weakening` ·
`skip_mock_injection` · `file_state` · `git_state` · `scope_drift` · `tool_call_provenance`.

A check that can't run returns `UNSUPPORTED`/`INDETERMINATE` **with a reason**. Degrading honestly
instead of guessing is the defining property of this codebase, and the rest of this section is
where that costs us something.

Some checks reason about *this turn* (`command_execution`, `file_state`, `git_state`,
`scope_drift`) — "did what just happened land?". Others reason across the whole session
(`test_freshness`, `test_provenance`, `assertion_weakening`, `skip_mock_injection`) — a source
edited three turns ago and never retested really is stale. So a `STALE` caused by an earlier turn
says exactly that, rather than reading as an accusation about work you just did.

### Known blind spot: `uv run --with`

A test run wrapped as:

```sh
uv run --with pytest pytest -q
```

is **not currently recognized as a test runner**. `command_execution` and `test_freshness` then
report as though no test ran. In one measured session this was 29 real test runs with 2
recognized — the miss rate is high enough that if this is how you run tests, Tycho is close to
blind to them today.

The cause is narrow: the ephemeral-env wrappers (`uv run`, `uvx`, `poetry run`, `npx`, …) are
matched, but only a multi-word runner phrase later in the segment counts, so `uv run pytest -q`
and `uv run --with pytest python -m pytest` are both seen while `uv run --with pytest pytest -q`
is not. A fix is queued and **not shipped**.

Until it ships:

```sh
tycho exec -- uv run --with pytest pytest -q
```

`tycho exec` doesn't parse the command at all — it runs it and records what the process returned —
so it is immune to every wrapping question, not just this one.

### `tool_call_provenance` is narrow and advisory

`tool_call_provenance` catches an agent that *claims* a tool action it never took. It currently
recognizes two families of claim — web search/fetch and issue-tracker actions (Jira/Linear).
Broader tool-call coverage — calendar, email, file/drive, code execution, and other connectors —
is **not yet supported and is in development**; until then a claim it can't classify is left
`UNSUPPORTED`, never guessed.

This is a **scope limitation, not a finished feature** — treat its result as advisory for now. Two
consequences follow from how narrowly it's currently drawn, both of which we'll address as the
family table is widened:

- **Missed claims ("no tool-action claims recognized").** The claim-recognition patterns are
  narrower than real phrasing, so a turn that genuinely performed a supported action can still
  read as *no claim recognized* — e.g. issue-tracker verbs like "parented", "linked", "added N
  links", or "created ticket X" that fall outside the pattern set. That's a recall gap, not a
  statement that no tools ran.
- **Coarse (potentially false-positive) matches.** Within a supported family the match is **broad
  — family-presence only, with no content-correlation.** A claim is considered backed if *some*
  tool call of that family occurred; it does not yet verify that the specific claimed action (that
  ticket, that URL) is the one the tool actually touched. So a claim could be marked backed by an
  unrelated call in the same family.

Both are deliberate: the check is tuned to **never emit a false FAIL** today, at the cost of these
gaps. Tightening the phrasing coverage and adding safe content-correlation is planned, not shipped
— so for now, read `tool_call_provenance` as a hint, and confirm from the transcript when it
matters.

### Other things Tycho will not claim

- **`blame PATH:LINE` is file-level.** It prints the note before the results, not after — a reader
  who takes the first row as line-42 attribution has already been misled by the time a footnote
  arrives.
- **`review` speaks about the record, not about execution.** "No command Tycho recorded ran after
  this hunk was written" — never "this line never executed".
- **`attest --verify` can answer *cannot tell*.** A pruned record is not a forged one.
- **A missing heartbeat is not proof the hook is dead.** A fresh install hasn't fired yet.
  `doctor` reports that as `INFO`, not `BROKEN`.
- **Nothing polls in the background.** A hook that died five minutes ago goes undiagnosed until
  someone asks. Tycho stays a thing that runs when called.

## Scope — bounding what the agent may edit

Tycho is **zero-config**: with no `.tycho.toml`, `scope_drift` reports `UNSUPPORTED` and every
other check runs exactly as normal. Nothing assumes the file exists.

Opt in when you want to bound *where* the agent edits. `tycho init` drops a starter `.tycho.toml`
(empty, so behaviour is unchanged until you add a glob), and you manage the allowlist with:

```
tycho scope add 'src/**' 'tests/**'   # allow these; an edit elsewhere FAILs scope_drift
tycho scope remove 'tests/**'         # drop one or more
tycho scope set 'src/**'              # replace the whole list
tycho scope list                      # show the current bounds

tycho scope set '**'                  # allow the whole tree…
tycho scope add --exclude 'LICENSE'   # …but carve this back out (exclude wins)
```

`set`, `add`, and `remove` each take **one or more** globs — quote them so your shell keeps them
literal. The bound is an **explicit, deterministic** declaration: Tycho stores exactly what you
wrote and never infers scope from the prompt. Set it once as a standing bound, or change it
whenever the task changes.

**Excluding paths.** Add `--exclude` to `set`/`add`/`remove` to edit a **denylist** instead of the
allowlist: a path that matches `exclude` FAILs `scope_drift` even if it also matches `include` —
exclude wins. An empty exclude is a pure allowlist; exclude has no effect while `include` is empty
(scope stays zero-config `UNSUPPORTED`, and `tycho scope list` says so).

## In Claude Code

`tycho init` wires Claude Code up with a status badge and slash commands, so you can drive Tycho
without leaving the session. Everything here also runs as a plain `tycho <command>` — the slash
commands are just the in-editor shortcut.

**Slash commands.** Type `/` and each shows with its own description:

| Command | Does |
|---|---|
| `/tycho <args>` | run any Tycho subcommand, e.g. `/tycho show` or `/tycho review --since HEAD~3` |
| `/tycho-verify` | verify the latest session and render a verdict |
| `/tycho-status` | the one-line badge (what the status bar renders) |
| `/tycho-doctor` | full diagnostics: is Tycho installed, current, and firing? |
| `/tycho-help` | what Tycho is, whether it's live here, and every command |
| `/tycho-count` | how many problems Tycho has caught here, and all-time |
| `/tycho-hide` · `/tycho-show` | hide / show the status badge |
| `/tycho-relay` · `-on` · `-off` | show / turn on / turn off the verdict relay (see [below](#let-the-agent-see-its-own-verdict--the-relay)) |
| `/tycho-override` · `-on` · `-off` · `-veto` | show / turn on / turn off the agent verdict override, or veto one |
| `/tycho-scope-list` · `-set` · `-add` · `-remove` | show or edit the files the agent may edit |

**Status badge.** A `[TYCHO]` indicator in the status bar, coloured by the last run:

| Color | Meaning |
|---|---|
| 🟢 green | last run `VERIFIED` |
| 🔴 red | last run `FAILED` or `STALE` — something to look at |
| 🔵 blue | verifying right now — a run is in flight this turn |
| 🟡 yellow | an `INDETERMINATE` run (ran, couldn't conclude) |
| 🟦 teal | last run `OVERRIDDEN` — agent-authorized, not proven |
| ⚪ grey | nothing to say yet — never fired here, `UNSUPPORTED`, or a turn with nothing to verify |

It settles on green or red once a real verdict exists; blue is only the in-flight moment, yellow
is the inconclusive-but-noteworthy run, and grey is the honest "no signal" — including a fresh
install that hasn't fired yet. The colors are muted on purpose, to stay readable on a dark
terminal.

`tycho verify` updates the badge too. If you already run a status line — a third-party badge, a
shell prompt — Tycho **composes** with it instead of replacing it: it takes Claude Code's single
`statusLine` slot, runs your existing command too, and renders both (`[OTHER] [TYCHO]`). Nothing
you had is lost, and `tycho uninstall` restores it.

**Toggle the badge.** `/tycho-hide` (or `tycho statusline --off`) hides only Tycho's segment — the
hook keeps verifying every turn; `/tycho-show` (or `tycho statusline --on`) brings it back.
`TYCHO_STATUS=off` hides it everywhere for a session.

## The badge in any shell

The badge isn't tied to the editor: `tycho statusline` reads `.tycho/` off disk and prints one
line, whichever agent wrote the state. Call it from anything that renders a command:

```sh
PS1='$(tycho statusline) '$PS1                                        # bash / zsh prompt
set -g status-right '#(cd #{pane_current_path} && tycho statusline)'  # tmux (in tmux.conf)
```

```toml
[custom.tycho]                                # starship (in starship.toml)
command = "tycho statusline"
when = true
```

No stdin needed — with no JSON on stdin it resolves the repo from the current directory, walking
up to find `.tycho/`, so the badge stays put as you `cd` around the tree. It prints nothing in
repos where Tycho isn't installed, so it stays out of every other prompt you have.

## Let the agent see its own verdict — the relay

By default Tycho's verdict is **human-only**: it renders to your terminal and never enters the
model's context, so the agent can't see when its own turn came back `FAILED` or `STALE`. That's
the safe default — **Tycho never spends your context or tokens unless you ask it to.**

Turn on the **verdict relay** and Tycho feeds a non-`VERIFIED` verdict back to the agent as it
finishes a turn, so the agent keeps working **until the verdict is `VERIFIED`** — catching a false
"done" the moment it happens instead of waiting for you to paste the verdict back in. `tycho init`
asks once (default **no**) when a repo has no `.tycho.toml` yet. Flip it any time:

```sh
tycho relay            # show the current setting
tycho relay --on       # feed non-VERIFIED verdicts back to the agent (works until VERIFIED)
tycho relay --off      # back to human-only (the default)
```

The setting is a hand-editable, per-repo key in `.tycho.toml` (`[relay] enabled`). Turning it on
also opts you out of the digest's novelty decay on unproven turns: you elected to be told, so you
get told every time, including the turn where the leash runs out.

**Agent override.** Off by default, and it requires the relay to be on — an override only exists
to break a relay loop, so there's nothing for it to do while the agent never sees its verdict.
Turn it on with `tycho override --on`; back off with `tycho override --off`. Stored per-repo in
`.tycho.toml` (`[override] enabled`).

With override on, the agent may record a justified per-check override — `tycho override <check>
"<reason>"` — logged to `.tycho/overrides.json` and shown to you. It can never hide a real
failure: any check still genuinely `FAILED` or `STALE` keeps that verdict regardless of what else
gets overridden. Only when *every* adverse check has been overridden does the turn resolve to the
distinct, logged `OVERRIDDEN` verdict (teal badge) — never `VERIFIED`.

**Your controls.** An `OVERRIDDEN` verdict shows you a human-only line naming the checks the agent
set aside, plus two levers: **veto** it — `tycho override --veto` — so the relay fires again and
the agent has to actually satisfy the check next turn; or **turn override off** entirely. A veto
persists across turns until lifted with `tycho override --unveto <check>`. Both are logged.

**It is bounded — no infinite loops.** Each user turn can be auto-continued at most **3 times**
before Tycho goes quiet and hands control back to you, so a verdict the agent can't satisfy
converges on a hard stop, not an endless cycle. A fresh prompt from you resets the leash. Change
the ceiling with `TYCHO_RELAY_MAX` (`0` disables the auto-continue entirely, leaving the verdict
as a one-shot note the agent sees but isn't pushed to act on).

**Estimated extra token usage.** With the relay **off** (default), **zero** — nothing reaches the
model. With it **on**, each turn that ends non-`VERIFIED` costs the injected verdict itself
(roughly **120–200 tokens** per re-check) plus **up to `TYCHO_RELAY_MAX` extra agent turns**
(default 3) — the real cost, since each is a full generation. Budget on the order of *your agent's
normal per-turn token use × up to 3* for a turn Tycho keeps re-checking; a turn that's already
`VERIFIED` adds **nothing**.

## Is the hook still firing?

A silently dead hook is the worst failure a verifier has — silence looks exactly like "everything
passed." `tycho doctor` checks, without editing anything, that Tycho's entry is still in each
harness's config, that the command it would run resolves to a real executable, that the install's
schema matches this Tycho, and when the hook last fired:

```sh
tycho doctor    # exits 0 when healthy, 5 when installed-but-not-working
```

```
tycho doctor (v0.1.0 · latest 0.1.0)
  ✓ claude: hook installed and runnable — /Users/you/.local/bin/tycho hook
  ✓ hook last fired 2m ago (via claude)
  ✓ most recent session: claude (/Users/you/.claude/projects/-Users-you-repo/….jsonl)
  ⚠ claude: hook contract verified against 2.1.210, you have 2.1.220 (Claude Code)

  healthy
```

Repair is always the same self-healing `tycho init`. `tycho verify` runs the config half of this
check too, warning on stderr if the hook is broken.

That last line is worth understanding: `doctor` proves the hook **fires**, not that the harness
**understood** its output. A harness can change its hook contract in a patch release and nothing
upstream tells us — the hook would still fire, still record a heartbeat, and the verdict would
reach nobody. The pinned version is the only thing that closes that gap, so drift is surfaced
where someone is already looking.

## Configuration

Per-repo settings live in `.tycho.toml` (hand-editable; `scope`, `relay`, `override`). Per-repo
state lives in `<repo>/.tycho/` — the tally, the heartbeat, `turns.jsonl` (the turn record),
`commands.jsonl` (`tycho exec` evidence), and `overrides.json`. `tycho init` gitignores it.

Command strings, check evidence and the agent's prose are **filtered for secrets before they hit
disk** — a match becomes a visible `[REDACTED]` so a reader knows something was removed — and
every field is truncated, so one pathological turn can't write a megabyte line.

That filter is best-effort, not a guarantee. It is pattern-based: it catches the common shapes
(cloud keys, tokens, connection strings, `KEY=value` assignments), and a credential in a shape
it doesn't recognise lands on disk intact. Treat `.tycho/` as containing whatever the agent
said and ran, and keep it out of git regardless — `tycho init` gitignores it, and `tycho doctor`
tells you if it isn't ignored, or if it was already committed before the ignore rule existed
(which `tycho init` cannot undo).

| Variable | Does |
|---|---|
| `TYCHO_HOME` | Tycho's machine-level root (the all-time tally). Falls back to `XDG_DATA_HOME/tycho`, then `~/.local/share/tycho` |
| `TYCHO_CLAUDE_HOME` | where Claude Code's sessions live. Claude's own `CLAUDE_CONFIG_DIR` is honored too |
| `TYCHO_STATUS` | `off` hides the status badge everywhere for a session |
| `TYCHO_RELAY_MAX` | ceiling on relay auto-continuations per user turn (default 3; `0` disables) |
| `TYCHO_TURNS_MAX` | how many turns `turns.jsonl` keeps |
| `TYCHO_COMMANDS_MAX` | how many runs `commands.jsonl` keeps (default 500) |
| `TYCHO_NO_UPDATE_CHECK` | suppress the update check entirely |
| `TYCHO_INSTALL` | how Tycho was installed, so `tycho update` picks the right channel |

Everything else Tycho knows is per-repo. The all-time tally is the one fact that isn't.

## Uninstall

```sh
tycho uninstall                  # remove Tycho's hooks from this repo
tycho uninstall --harness claude # just one
tycho uninstall --global         # remove the machine-wide install instead
tycho uninstall --purge          # hooks, plus this repo's .tycho/ state and .tycho.toml config
```

Only Tycho's own entries come out — your other hooks, and any unrelated settings in the same file,
are left exactly as they were. The `prepare-commit-msg` block and the `.gitignore` entry come out
too, restoring the files byte-for-byte. It's idempotent, so running it twice is safe. A repo-local
uninstall deliberately leaves a `--global` install alone, and vice versa: two separate, explicit
decisions.

By default uninstall leaves `.tycho/` and `.tycho.toml` behind. Add **`--purge`** to delete those
too — an explicit opt-in, never the default, since it drops your catch history. It stays
repo-local; the machine-wide all-time tally is never touched.

Removing the hooks and removing the package are two separate steps: uninstall first (otherwise the
harness is left calling a command that no longer exists), then remove the package the same way you
installed it — `uv tool uninstall tycho-cli`, `pipx uninstall tycho-cli`, `pip uninstall
tycho-cli`, `brew uninstall tycho`, or deleting the standalone binary. (The distribution is
`tycho-cli`, not `tycho`.)

## How it works

The engine is pure and harness-agnostic: `gather → check → verdict` over an immutable snapshot,
with all I/O at the edges. Harness-specific differences — transcript format, which payload field
names the repo, which output channel reaches a human rather than the model — are isolated in one
adapter, so more harnesses can be added without touching the engine.

The hook itself is deliberately small and paranoid. **It always exits 0 and never blocks your
agent** — malformed stdin, an unreadable transcript, a git hiccup all mean "return nothing, exit
0". A verifier that can break a turn is one people uninstall. Only `tycho verify` (and the opt-in
`review --exit-code` / `attest --verify` gates) exits non-zero, so CI can gate on it.

The turn record behind `show`, `blame`, `log`, `review`, `attest` and `count --ledger` is one
append-only JSONL file — greppable, stdlib-readable, no index. That is what buys the zero
dependencies, and the zero dependencies are what let all of this be instant and free.

## License

Apache-2.0.

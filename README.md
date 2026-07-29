<div align="center">

<img src="assets/swail-logo-rounded.png" alt="Swail" width="96">

# Tycho

**by [Swail](https://swail.dev)**

The system of record for what your coding agents did.

[![CI](https://github.com/swail-labs/tycho/actions/workflows/ci.yml/badge.svg)](https://github.com/swail-labs/tycho/actions)
[![PyPI](https://img.shields.io/pypi/v/tycho-cli)](https://pypi.org/project/tycho-cli/)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Deps](https://img.shields.io/badge/deps-stdlib%20only-orange)](pyproject.toml)

</div>

---

> *"never accept the model's summary as evidence, only artifacts it can't fake by narrating"*

Tycho runs as a completion hook on your agent's harness. It records what every turn actually
did — files touched, commands run and what they returned, what the agent claimed — and checks
that against the artifacts: git, the filesystem, exit codes, the harness event stream.

**Only a hook on the event stream sees the process.** A diff sees a result. CI sees a result. A
second model sees a diff. Tycho was there when the agent ran `pytest`, got exit 1, and then said
tests pass.

Instant, free, silent, always-on — no API key, no second session, no tokens, no context burned.
Nothing leaves the machine (no LLM anywhere in the trust path). Stdlib only, Python ≥ 3.11.
Claude Code and Codex today; other harnesses in development. Linux, macOS, Windows — WSL2 counts as Linux,
install inside the distro.

## Install

```sh
curl -fsSL https://swail.dev/tycho/install.sh | sh   # standalone binary, no Python
brew install swail-labs/tap/tycho                    # same binary, upgradable
uv tool install tycho-cli                            # have Python? (pipx works too)
npx @swail-labs/tycho <command>                      # npm wrapper, same binary
```

The PyPI package is **`tycho-cli`** (`tycho` was taken); it installs the **`tycho`** command.
Pick one channel per machine — `npm -g` and `brew` both want to own `bin/tycho`.

## Setup

```sh
tycho init              # wire up this repo (asks per harness; idempotent, self-healing)
tycho init --yes        # skip the prompts
tycho init --global     # every repo; defers to any per-repo install
```

One run wires the completion hook, a status-bar badge and `/tycho` slash commands, a `.tycho/`
entry in `.gitignore`, and a `prepare-commit-msg` hook that stamps a `Tycho-Attestation:` trailer
onto commits an agent worked on. It merges with hooks you already have, backs up what it changes,
and refuses to touch a config it can't parse. Undo with `tycho uninstall`.

That's it. The next time an agent finishes a turn, Tycho verifies it.

## After a turn

Most turns print nothing. Tycho speaks when a check says the turn isn't proven, when the prose
claims done but the evidence doesn't reach it, when a run of proven turns breaks, or when a turn
touches far more files than this repo's turns usually do. A condition that fired the last two
turns goes quiet, so a standing failure doesn't repeat at you forever.

```
🔍 Tycho: FAILED — file_state: docs/product-direction.md — missing
   ✓ attempted  · executed  ✓ artifact_changed  ✓ claim_supported
   2 files · pytest -q… failed · +7 more
   `tycho show` for the full receipt
```

The second line is the **acceptance ladder**: `attempted → executed → artifact_changed →
claim_supported`. Unreached rungs render `·`, not `✗` — a gap in the evidence isn't a failure.
Verdicts are `VERIFIED` · `FAILED` · `STALE` (evidence exists but predates the code) ·
`INDETERMINATE` (ran, couldn't conclude) · `UNSUPPORTED` · `OVERRIDDEN`.

## What you can do with it

```sh
tycho show                       # the full receipt for the last turn (or `show <id>`)
tycho log                        # what agents did here, newest first (--verdict, --since, -n)
tycho blame src/app.py           # which turns touched this file, what they claimed, what backed it
tycho review                     # which changed hunks no test covered and no command exercised
tycho backfill                   # record what agents did here before Tycho was installed
tycho verify                     # re-run the engine over a session on demand
tycho count --ledger             # catch and blind rates, per model and per check
tycho exec -- pytest -q          # run it and put the real exit status on the record
tycho attest --verify            # check a commit's trailer against the record
tycho scope add 'src/**'         # bound where the agent may edit (opt-in; `--exclude` denies)
tycho relay --on                 # let the agent see its own verdict and keep working (off by default)
tycho rewrite --on               # route a piped runner through `tycho exec` for you (off by default)
tycho doctor                     # is the hook installed, current, and firing?
```

`tycho help` lists everything, including `hook`, `init`, `uninstall`, `statusline`, `run`,
`override` and `update`.

**Exit codes** — one per adverse kind, so a gate picks what blocks. `1` FAILED, `3` STALE
(`verify`); `6` unexercised (`review --exit-code`); `7` trailer mismatch (`attest --verify`); `5`
installed-but-broken (`doctor`); `2` usage, `4` couldn't complete. The hook itself always exits 0
and never blocks your agent.

## What Tycho won't claim

Tycho answers *can't tell* rather than guessing, so there are places it deliberately says less
than you might expect:

- **`blame PATH:LINE` is file-level.** The record stores which turns touched a file, never which
  lines. Joining through `git blame` would put a confident wrong name on a line.
- **`review` speaks about the record, not execution.** "No command Tycho recorded ran after this
  hunk" — never "this line never ran". There's no per-line coverage here.
- **The attestation proves consistency, not authenticity.** Unkeyed SHA-256 over a record this
  machine wrote: tamper-evidence against accidents, not a defence against a motivated agent.
  `--verify` can answer *cannot tell*, and a pruned record must never read as a forged one.
- **`tool_call_provenance` is advisory.** Two claim families (web, issue trackers), matched on
  family presence only. Tuned to never emit a false FAIL, at the cost of recall.
- **A backfilled turn has no verdict, and never will.** `tycho backfill` replays transcripts
  written before Tycho was here, but most checks are functions of state that no longer exists —
  whether an edited file is on disk *now*, an mtime *now* against a run three weeks ago. So those
  rows record what the transcript proves (files, commands, claims, when) and read `UNVERIFIED`.
  They're excluded from every rate in `count --ledger`. Tycho will not tell you what it "would
  have caught".
- **`verifier_integrity` raises the cost of tampering; it doesn't make it impossible.** It reports
  a turn that edits `.tycho/`, `.tycho.toml`, a harness hook config or a git hook — via the edit
  tools, a shell redirect, or a mutating command. It cannot see a write smuggled through an
  interpreter (`python -c "open('.tycho.toml','w')"`) or a path the shell expanded from a
  variable, and a process running as you can always reach the file. Tycho's own state is judged
  inside this repo only — another tree's `.tycho/` is `scope_drift`'s business, not a verdict
  here. It is the check `[checks].disable` may not switch off.
- **A piped runner's exit status is gone before Tycho can read it.** `pytest | tail -20` hands the
  harness tail's status, so the verdict falls back to reading pytest's summary line — inference,
  and only as good as the vocabulary in `engine/runlog.py`. `tycho exec -- pytest | tail -20`
  fixes it at the source, and `tycho rewrite --on` does that for you. Rewriting is opt-in because
  Claude Code matches your `Bash(...)` rules against the rewritten command, so an allow rule you
  already granted can stop covering it; under `--permission-mode bypassPermissions` there are no
  rules to void and it always applies.
- **Nothing polls in the background.** A hook that died goes undiagnosed until someone runs
  `doctor`.

## Configuration

Settings in `.tycho.toml` (`scope`, `relay`, `override`, `rewrite`), state in `<repo>/.tycho/`. Command
strings, evidence and agent prose are pattern-filtered for secrets before hitting disk — a match
becomes a visible `[REDACTED]` — but it's best-effort, so keep `.tycho/` out of git regardless.

`TYCHO_HOME` · `TYCHO_CLAUDE_HOME` · `TYCHO_STATUS` · `TYCHO_RELAY_MAX` · `TYCHO_TURNS_MAX` ·
`TYCHO_COMMANDS_MAX` · `TYCHO_NO_UPDATE_CHECK` · `TYCHO_INSTALL`.

The one thing that touches the network: a SessionStart hook asks `pypi.org` once a day whether a
newer Tycho exists, disclosing your version and IP. `TYCHO_NO_UPDATE_CHECK=1` turns it off.

## License

Apache-2.0. Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

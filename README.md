<div align="center">

<img src="docs/swail-logo-rounded.png" alt="Swail" width="96">

# Tycho

**by [Swail](https://swail.dev)**

The local verifier that proves your AI coding agent actually did what it claims.

[![CI](https://github.com/swail-labs/tycho/actions/workflows/ci.yml/badge.svg)](https://github.com/swail-labs/tycho/actions)
[![PyPI](https://img.shields.io/pypi/v/tycho-cli)](https://pypi.org/project/tycho-cli/)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Deps](https://img.shields.io/badge/deps-stdlib%20only-orange)](pyproject.toml)

</div>

---

A free, offline, open-source **local verifier** that proves an AI coding agent actually did what it
claims — from git, the filesystem, process exit codes, and the harness event stream. When an agent
says "done," Tycho turns some ✅s into `FAILED` / `STALE` / `INDETERMINATE` with per-check evidence,
*before* that ✅ reaches you.

- **No account, no network, no LLM in the trust path.** Only code renders a verdict.
- **Stdlib only**, Python ≥ 3.11. Nothing to audit, trivial install.
- **Claude Code** supported (auto-detected); more harnesses coming.
- **Linux, macOS, and Windows** — see [Platforms](#platforms).

## Platforms

| OS | Status | Verified by |
|----|--------|-------------|
| Linux | Supported | CI: `ubuntu-latest`, Python 3.11 / 3.12 / 3.13 — full suite, `ruff`, and a wheel + sdist install into a clean venv |
| Windows | Supported | CI: `windows-latest`, Python 3.12 — same suite, plus the `.exe` console-script hook under cp1252/backslash |
| macOS | Supported | Development baseline; not gated in CI |

**WSL2 is Linux** (a real Linux kernel), so it's covered by the Linux row. Install and run Tycho
*inside* the distro — launch your agent from the WSL shell — rather than driving `wsl.exe` from a
Windows-hosted agent, so Tycho's Stop hook and the commands it verifies share one environment.

## Install

Fastest — the standalone binary, no Python needed:

```sh
curl -fsSL https://swail.dev/tycho/install.sh | sh
```

It downloads the binary for your platform, verifies it against the release checksums, and drops
the `tycho` command in `~/.local/bin`.

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

Then wire it into your repo (or let Tycho offer to on first run):

```sh
tycho init
```

## Quickstart

```sh
tycho init              # install the completion hook into the harnesses you actually have
                        # (asks per harness; idempotent, self-healing, repo-local only)
tycho init --yes        # skip the prompts (scripts and CI)
```

Tycho only touches Claude Code, and only inside this repo. It merges with hooks you already have, backs
up anything it changes, and refuses to touch a config it can't parse rather than risk it. (Cursor, Codex,
and OpenCode support is in development.)

It also adds a status-bar badge and a set of `/tycho` slash commands, so you can see it's on and drive it
without leaving the session — see [In Claude Code](#in-claude-code) below. `tycho statusline` also prints
that badge for a shell prompt or tmux status line — see [The badge in any shell](#the-badge-in-any-shell).

That's it. The next time an agent finishes a turn, Tycho verifies it automatically and prints the
verdict. To run it by hand:

```sh
tycho verify                     # auto-discovers the most-recently-used session and verifies it
tycho verify --harness claude    # force a harness (default: whichever ran most recently)
tycho verify --session <path>    # verify a specific transcript
tycho verify --claim "added rate limiting, tests pass"   # echo the claim above the verdict
tycho help                       # what Tycho is, whether it's live here, and every command
tycho statusline                     # the one-line status-bar indicator (what Claude Code renders)
tycho count                      # how many problems Tycho has caught — in this repo, and all-time
tycho run -- pytest -q           # run a command so its true exit code is seen (see "tycho run" below)
tycho scope list                 # show/edit which files the agent may edit (see "Scope" below)
tycho relay                      # let the agent see its verdict & work till VERIFIED (opt-in; see below)
tycho --version                  # print the version
tycho --help                     # full usage (also `tycho verify --help`)
```

```
🔍 Tycho: FAILED
  ✗ test_freshness — auth_test.py passed 14:22:01, auth.py edited 14:22:47 (46s later)
  ✗ assertion_weakening — 2 assertions removed from test_login_expiry
  ✓ command_execution — pytest ran, exit 0
  • scope_drift — no [scope] set — run `tycho scope add '<glob>'` to bound edits (zero-config)
```

`tycho verify` exits non-zero on an adverse finding so you can gate in `pre-push`/CI. The completion
hook never blocks — it annotates; you decide.

| Exit | Meaning |
|---|---|
| `0` | `VERIFIED` / `UNSUPPORTED` / `INDETERMINATE` — nothing adverse found |
| `1` | `FAILED` — a check proved the claim wrong |
| `2` | usage error |
| `3` | `STALE` — edits landed after the last passing test run |
| `4` | Tycho could not complete (unreadable transcript/config) |

`FAILED` and `STALE` are separate codes so you can pick which one blocks: gate on `1` alone, or on
both.

## `tycho run` — when a test run is hidden from Tycho

Tycho recognises a test/build run from the command in the transcript. It reads *through* the common
ways a runner is wrapped — a pipe (`pytest | tail`), a variable interpreter (`"$PY" -m pytest`), and
nested shells (`wsl.exe -d Ubuntu -- bash -c 'pytest'`, `ssh host 'pytest'`) — automatically, with
nothing to remember. **WSL runs verify on their own; you don't need `tycho run` for them.**

The one case it can't see is when the runner's name isn't in the command at all — a Makefile target
(`make test`), a shell alias, or a `./run-tests.sh` that calls pytest inside. For those, prefix it:

```sh
tycho run -- make test
```

`tycho run` execs the command, forwards its output and exit code **unchanged**, and lets Tycho read
the runner's true status. It's a thin, safe wrapper — never use it as a habit for WSL or piped runs,
only where the runner is genuinely hidden behind indirection.

## Scope — bounding what the agent may edit

Tycho is **zero-config**: with no `.tycho.toml`, `scope_drift` reports `UNSUPPORTED` and every other
check runs exactly as normal. Nothing assumes the file exists.

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
literal. The bound is an **explicit, deterministic** declaration: Tycho stores exactly what you wrote
and never infers scope from the prompt. Set it once as a standing bound, or change it whenever the task
changes. In Claude Code the same four are `/tycho-scope-list`, `/tycho-scope-set`, `/tycho-scope-add`,
and `/tycho-scope-remove`.

**Excluding paths.** Add `--exclude` to `set`/`add`/`remove` to edit a **denylist** instead of the
allowlist: a path that matches `exclude` FAILs `scope_drift` even if it also matches `include` — exclude
wins. This lets you allow a broad area and carve specific files back out (e.g. `include = ["**"]`,
`exclude = ["LICENSE"]` allows the whole repo but that one file). An empty exclude is a pure allowlist,
exactly as before; exclude has no effect while `include` is empty (scope stays zero-config `UNSUPPORTED`).

## In Claude Code

`tycho init` wires Claude Code up with a status badge and slash commands, so you can drive Tycho without
leaving the session. Everything here also runs as a plain `tycho <command>` in your terminal — the slash
commands are just the in-editor shortcut.

**Slash commands.** Type `/` and each shows with its own description:

| Command | Does |
|---|---|
| `/tycho <args>` | run any Tycho subcommand, e.g. `/tycho verify --claim "added rate limiting"` |
| `/tycho-verify` | verify the latest session and render a verdict |
| `/tycho-status` | the one-line badge (what the status bar renders) |
| `/tycho-doctor` | full diagnostics: is Tycho installed, current, and firing? |
| `/tycho-help` | what Tycho is, whether it's live here, and every command |
| `/tycho-count` | how many problems Tycho has caught here, and all-time |
| `/tycho-hide` · `/tycho-show` | hide / show the status badge |
| `/tycho-relay` · `-on` · `-off` | show / turn on / turn off the verdict relay (see [below](#let-the-agent-see-its-own-verdict--the-relay)) |
| `/tycho-override` · `-on` · `-off` · `-veto` | show / turn on / turn off the agent verdict override, or veto one (see [below](#let-the-agent-see-its-own-verdict--the-relay)) |
| `/tycho-scope-list` · `-set` · `-add` · `-remove` | show or edit the files the agent may edit (each `set`/`add`/`remove` takes one or more globs) |

**Status badge.** A `[TYCHO]` indicator in the status bar, coloured by the last run:

| Color | Meaning |
|---|---|
| 🟢 green | last run `VERIFIED` |
| 🔴 red | last run `FAILED` or `STALE` — something to look at |
| 🔵 blue | verifying right now — a run is in flight this turn |
| 🟡 yellow | an `INDETERMINATE` run (ran, couldn't conclude) |
| 🟦 teal | last run `OVERRIDDEN` — agent-authorized, not proven |
| ⚪ grey | nothing to say yet — never fired here, `UNSUPPORTED`, or a turn with nothing to verify |

It settles on green or red once a real verdict exists; blue is only the in-flight moment,
yellow is the inconclusive-but-noteworthy run, and grey is the honest "no signal" — including a
fresh install that hasn't fired yet. The colors are muted on purpose, to stay readable on a dark
terminal.

`tycho verify` updates the badge too, so a manual verify that comes back `VERIFIED` turns it green. If you
already run a status line — a third-party badge, a shell prompt — Tycho **composes** with it instead of
replacing it: it takes Claude Code's single `statusLine` slot, runs your existing command too, and renders
both (`[OTHER] [TYCHO]`). Nothing you had is lost, and `tycho uninstall` restores it.

**Toggle the badge.** `/tycho-hide` (or `tycho statusline --off`) hides only Tycho's segment — the hook keeps
verifying every turn; `/tycho-show` (or `tycho statusline --on`) brings it back. `TYCHO_STATUS=off` hides it
everywhere for a session.

## Let the agent see its own verdict — the relay

By default Tycho's verdict on Claude Code is **human-only**: it renders to your terminal and never enters
the model's context, so the agent can't see when its own turn came back `FAILED` or `STALE`. That's the
safe default — **Tycho free never spends your context or tokens unless you ask it to.**

Turn on the **verdict relay** and Tycho feeds a non-`VERIFIED` verdict back to the agent as it finishes a
turn, so the agent keeps working **until the verdict is `VERIFIED`** — catching a false "done" the moment
it happens instead of waiting for you to paste the verdict back in. `tycho init` asks once (default **no**)
when a repo has no `.tycho.toml` yet; if you already have one, it leaves your setting alone and just points
you at the toggle. Flip it any time:

```sh
tycho relay            # show the current setting
tycho relay --on       # feed non-VERIFIED verdicts back to the agent (works until VERIFIED)
tycho relay --off      # back to human-only (the default)
```

In Claude Code the same three are `/tycho-relay`, `/tycho-relay-on`, and `/tycho-relay-off`; bare
`/tycho-relay` shows the current status and how to toggle. The setting is a hand-editable, per-repo key in
`.tycho.toml` (`[relay] enabled`). It works on Claude Code today; other harnesses are in development.

**Agent override.** Off by default, and it requires the relay to be on — an override only exists
to break a relay loop, so there's nothing for it to do while the agent never sees its verdict.
Turn it on with `tycho override --on` (or `/tycho-override-on` in Claude Code); back off with
`tycho override --off` (`/tycho-override-off`). Stored the same way as the relay, per-repo in
`.tycho.toml` (`[override] enabled`).

With override on, the verdict the relay feeds back tells the agent it may record a justified
per-check override — `tycho override <check> "<reason>"` — logged to `.tycho/overrides.json` and
shown to you. It can never hide a real failure: any check that's still genuinely `FAILED` or
`STALE` keeps that verdict regardless of what else gets overridden. Only when *every* adverse
check has been overridden does the turn resolve to the distinct, logged `OVERRIDDEN` verdict (teal
badge) — never `VERIFIED`.

**What you see, and your controls.** An `OVERRIDDEN` verdict shows you a human-only line naming
the checks the agent set aside, plus two levers: **veto** it — `tycho override --veto`
(`/tycho-override-veto`) — so the relay fires again and the agent has to actually satisfy the
check next turn; or **turn override off** entirely — `tycho override --off`
(`/tycho-override-off`). `tycho override --unveto` lifts a previous veto so that check can be
overridden again.

**It is bounded — no infinite loops.** Each user turn can be auto-continued at most **3 times** before
Tycho goes quiet and hands control back to you, so a verdict the agent can't satisfy converges on a hard
stop, not an endless cycle. A fresh prompt from you resets the leash. Change the ceiling with
`TYCHO_RELAY_MAX` (e.g. `TYCHO_RELAY_MAX=5`; `0` disables the auto-continue entirely, leaving the verdict as
a one-shot note the agent sees but isn't pushed to act on).

**Estimated extra token usage.** With the relay **off** (default), **zero** — nothing reaches the model.
With it **on**, each turn that ends non-`VERIFIED` costs:

- the injected verdict itself — small and fixed, roughly **120–200 tokens** per re-check (the verdict lines
  plus a one-paragraph guard), and
- **up to `TYCHO_RELAY_MAX` extra agent turns** (default 3) — the real cost, since each is a full generation
  in which the agent reads context and works. Budget on the order of *your agent's normal per-turn token
  use × up to 3* for a turn Tycho keeps re-checking; a turn that's already `VERIFIED` adds **nothing**.

So the relay trades tokens for the agent catching its own mistakes without you in the loop. Leave it off for
the cheapest, quietest default; turn it on when you'd rather the agent fix a bad turn before it reaches you.

## The badge in any shell

The badge isn't tied to the editor: `tycho statusline` reads `.tycho/` off disk and prints one line,
whichever agent wrote the state. Call it from anything that renders a command:

```sh
PS1='$(tycho statusline) '$PS1                                        # bash / zsh prompt
set -g status-right '#(cd #{pane_current_path} && tycho statusline)'  # tmux (in tmux.conf)
```

```toml
[custom.tycho]                                # starship (in starship.toml)
command = "tycho statusline"
when = true
```

No stdin needed — with no JSON on stdin it resolves the repo from the current directory, walking up to
find `.tycho/`, so the badge stays put as you `cd` around the tree. It prints nothing in repos where
Tycho isn't installed, so it stays out of every other prompt you have. `tycho statusline --off` / `--on` and
`TYCHO_STATUS=off` work here exactly as they do in Claude Code.

## Is the hook still firing?

A silently dead hook is the worst failure a verifier has — silence looks exactly like "everything
passed." `tycho doctor` checks, without editing anything, that Tycho's entry is still in each harness's
config, that the command it would run resolves to a real executable, and when the hook last fired:

```sh
tycho doctor    # exits 0 when healthy, 5 when installed-but-not-working
```

Repair is always the same self-healing `tycho init`. `tycho verify` runs the config half of this check
too, warning on stderr if the hook is broken. See [`docs/hooks.md`](docs/hooks.md#is-it-still-working-tycho-doctor).

## Uninstall

```sh
tycho uninstall                  # remove Tycho's hooks
tycho uninstall --harness claude # just one
tycho uninstall --purge          # hooks, plus this repo's .tycho/ state and .tycho.toml config
```

Only Tycho's own entries come out — your other hooks, and any unrelated settings in the same file,
are left exactly as they were. It's idempotent, so running it twice is safe.

By default uninstall leaves two repo-local files behind: `.tycho/` (the catch tally and evidence trail)
and `.tycho.toml` (your scope/checks config). Add **`--purge`** to delete those too — an explicit opt-in,
never the default, since it drops your catch history. It stays repo-local; the machine-wide all-time tally
under `~/.local/share/tycho` is never touched.

Removing the hooks and removing the package are two separate steps: uninstall first (otherwise the
harness is left calling a command that no longer exists), then remove the package the same way you
installed it — `uv tool uninstall tycho-cli`, `pipx uninstall tycho-cli`, `pip uninstall tycho-cli`,
or deleting the standalone binary. (The distribution is `tycho-cli`, not `tycho`.)

## The checks

`command_execution` · `test_freshness` · `test_provenance` · `assertion_weakening` ·
`skip_mock_injection` · `file_state` · `git_state` · `scope_drift` · `tool_call_provenance`. A check
that can't run returns `UNSUPPORTED`/`INDETERMINATE` with a reason — Tycho degrades honestly rather
than guess.

`tool_call_provenance` catches an agent that *claims* a tool action it never took. It currently
recognizes two families of claim — web search/fetch and issue-tracker actions (Jira/Linear). Broader
tool-call coverage — calendar, email, file/drive, code execution, and other connectors — is **not yet
supported and is in development**; until then a claim it can't classify is left `UNSUPPORTED`, never
guessed.

This is a **scope limitation, not a finished feature** — treat its result as advisory for now. Two
consequences follow from how narrowly it's currently drawn, both of which we'll address as the family
table is widened:

- **Missed claims ("no tool-action claims recognized").** The claim-recognition patterns are narrower
  than real phrasing, so a turn that genuinely performed a supported action can still read as *no claim
  recognized* — e.g. issue-tracker verbs like "parented", "linked", "added N links", or "created ticket
  X" that fall outside the pattern set. That's a recall gap, not a statement that no tools ran.
- **Coarse (potentially false-positive) matches.** Within a supported family the match is **broad —
  family-presence only, with no content-correlation.** A claim is considered backed if *some* tool call
  of that family occurred; it does not yet verify that the specific claimed action (that ticket, that
  URL) is the one the tool actually touched. So a claim could be marked backed by an unrelated call in
  the same family.

Both are deliberate: the check is tuned to **never emit a false FAIL** today, at the cost of these gaps.
Tightening the phrasing coverage and adding safe content-correlation is planned, not shipped — so for
now, read `tool_call_provenance` as a hint, and confirm from the transcript when it matters.

## How it works

The engine is pure and harness-agnostic: `gather → check → verdict` over an immutable snapshot, all I/O
at the edges. Harness-specific differences (transcript format, repo field, output channel) are isolated
in one adapter, so more harnesses can be added without touching the engine. See
[`docs/hooks.md`](docs/hooks.md) for the design.

Tycho looks for Claude Code's sessions under the usual `~/.claude`. If yours live elsewhere, set
`TYCHO_CLAUDE_HOME` to the directory that holds them — Claude's own `CLAUDE_CONFIG_DIR` is honored too.
See [`docs/hooks.md`](docs/hooks.md#overriding-where-an-agents-data-lives).

Tycho's own machine-level state — just the all-time tally behind `tycho count` — lives under
`~/.local/share/tycho`; set `TYCHO_HOME` (or `XDG_DATA_HOME`) to move it. Everything else Tycho
knows is per-repo, in `<repo>/.tycho/`.

## License

Apache-2.0.

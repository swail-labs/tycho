# Tycho — Completion Hooks (Claude Code + Cursor + Codex + OpenCode)

> How Tycho fires automatically when an agent says "done," and how one engine serves four
> harnesses whose transcripts look nothing alike.

## The shape

Claude Code, Cursor, and Codex call a **Stop hook** — a shell command that runs when a turn ends. They
hand it a JSON payload on **stdin** and read a JSON object back on **stdout**. Tycho registers
`tycho hook` as that command. The flow is always the same:

```
stdin JSON ──► detect harness ──► locate transcript + repo ──► gather → checks → verdict ──► stdout JSON
```

The **engine is harness-agnostic**. `checks`, `astdiff`, `verify`, `verdict_of` run on a normalized
`Session` and never learn which harness produced it. Everything harness-specific lives in one thin
adapter file — `tycho/harness.py` — plus one transcript reader per harness in `tycho/events.py`.

OpenCode exposes the same lifecycle point as the `session.idle` plugin event. Its plugin calls the
*same* `tycho hook` entrypoint as every other harness, handing it `{harness, sessionID, directory}` on
stdin. Tycho then rebuilds the session's transcript by reading OpenCode's own SQLite store
(`~/.local/share/opencode/opencode.db`) — no `opencode export`, which truncates at 128 KB whenever its
stdout is a pipe. The report is displayed with `client.tui.showToast`. OpenCode does not currently
expose a pre-stop hook that can cancel completion; Tycho only annotates completion, so no behavior is
lost.

## What actually differs (and what doesn't)

The engine-facing differences are isolated in the harness adapters:

| | **Claude Code** | **Cursor** | **Codex** | **OpenCode** |
|---|---|---|---|---|
| Completion signal | `Stop` | `stop` | `Stop` | `session.idle` |
| Transcript source | payload path | payload path | payload path | `opencode.db` (session id → rebuilt) |
| Output channel | `systemMessage` | `followup_message` | `systemMessage` | TUI toast |
| Transcript reader | `events.parse` | `events.parse_cursor` | `events.parse_codex` | `events.parse_opencode` |

Detection is by payload shape: Cursor carries `workspace_roots`/`cursor_version`; Codex carries the
documented `hook_event_name` and `turn_id`; otherwise Tycho defaults to Claude. See `harness.detect`.

Which harness version each of these was last *verified* against — and how to re-verify when one
drifts — lives in [`harness-support.md`](harness-support.md). A wrong output field here is invisible:
the hook fires, doctor reports healthy, and the verdict reaches nobody. That is not hypothetical; it
is what `user_message` did on Cursor for the life of the adapter.

`Harness` is a frozen record of those four differences:

```python
Harness(name, parse, repo_root, format_output, discover)
CLAUDE = Harness("claude",  events.parse,        cwd_root,    lambda t: {"systemMessage": t}, _claude_discover)
CURSOR = Harness("cursor",  events.parse_cursor, cursor_root, _cursor_output,                 _cursor_discover)
CODEX  = Harness("codex",   events.parse_codex,  cwd_root,    lambda t: {"systemMessage": t}, _codex_discover)
```

Cursor's output channel is the one that isn't a free lunch. Its stop hook reads exactly one key —
`followup_message` — and replays it as a *user message* into the model loop, so unlike Claude's
`systemMessage` there is no human-only channel: the agent sees the verdict. `_cursor_output` therefore
appends a relay instruction ("show this verbatim and end your turn"), keeping Tycho a reporter rather
than a commissioner of work. That's a prompt, not a guarantee; Cursor's own `loop_count`/`loop_limit`
is the hard backstop, and exit 0 still never blocks. Pinned in `tests/fixtures/cursor_stop_payload.json`.

There is a second, separate output channel: the **bootup update-notice** at session start
(`hook.session_start`), which rides `Harness.notice_output` — a *human-only* field, distinct from
`format_output` on purpose. It defaults to `None` ("no user-facing bootup channel"), so a notice is
suppressed rather than sent somewhere the model would read it and be commissioned to self-update
(the user-facing rule). Claude/Codex use `systemMessage`, OpenCode toasts `message`; Cursor — whose only
channels are model-facing — stays `None`. The per-harness SessionStart contract is in
[`harness-support.md`](harness-support.md).

Adding a harness later = one `Harness` entry + (if its transcript schema is new) one reader + one
pinned fixture. No engine change.

## Installing it (`tycho init`)

`tycho init` writes completion hooks at the **project level** — only ever `<repo>/.claude`, `.cursor`,
`.codex`, `.opencode`. Your global config is never touched. It merges (never clobbers hooks you already
configured) and is **self-healing**: re-running strips any existing tycho entry and rewrites it, so a
stale or broken command gets repaired in place — never duplicated. A file that's already correct isn't
rewritten at all.

**It only touches harnesses that are actually here, and asks first.** A harness counts as present if it
has run in this repo (its dotdir exists) or is installed for this user (its home exists — read only, as
a signal). Each detected harness gets its own prompt, defaulting to yes:

```sh
tycho init                      # prompt per detected harness
tycho init --yes                # no prompts — scripts and CI
tycho init --harness cursor     # just this one, detected or not
```

Without a tty and without `--yes` it installs nothing and says so: there's nobody to ask, and installing
unasked is the failure mode this design exists to prevent.

**It refuses rather than risk your config.** Malformed JSON is left exactly as-is — parsing it to `{}`
and writing that back is how an installer eats a developer's settings, so a trailing comma means "leave
this alone", not "replace it". Same for a read-only file, an unreadable one, a non-object at the top
level, or a hand-written `tycho.js` that isn't ours. Every refusal names the fix and exits `4`, so a
provisioning script fails loudly instead of leaving the repo silently unhooked. `uninstall` follows the
same rule — removing our hook is not worth risking the rest of the file.

Before any mutation the existing file is copied to `<name>.tycho.bak`, and writes go to a temp sibling
renamed over the target. Rename is atomic, so a run killed mid-write leaves the original whole rather
than a truncated husk. Permissions and unrelated keys are preserved, and symlinks are written *through*
(a settings file symlinked into a dotfiles repo stays a symlink).

**`uninstall` does not prompt, and that asymmetry is deliberate.** `init` asks per harness
because it writes *new* behavior into a config the user may not have meant to change — the surprise
this design exists to prevent. `uninstall` only ever removes Tycho's *own* entries (`_is_tycho_hook`), never
user content, and the user named the verb: running it *is* the consent. A prompt would also break the
one context that matters most for removal — a CI or teardown script tearing the hook back out
non-interactively — so `uninstall` stays promptless and needs no `--yes`. It still refuses (not prompts)
a config it can't safely parse, exactly like `init`.

**The command must run without an activated venv** — the Stop hook fires in a plain `/bin/sh`. A bare
`tycho hook` only works when `tycho` is on the global PATH (a `pipx`/global install). Otherwise
`init` resolves an absolute command so PATH doesn't matter: `shutil.which("tycho")` if found, else the
current interpreter — `<python> -m tycho.cli hook`. (Trade-off: an absolute path is machine-specific,
so a committed `settings.json` won't be portable to teammates; a global `pipx` install keeps it a
clean, committable `tycho hook`.)

```jsonc
// .claude/settings.json          // Stop is a list of matcher-groups, each with a `hooks` list
{ "hooks": { "Stop": [ { "hooks": [ { "type": "command", "command": "<resolved> hook" } ] } ] } }

// .cursor/hooks.json             // stop is a flat list; needs a top-level `version`
{ "version": 1, "hooks": { "stop": [ { "command": "<resolved> hook" } ] } }

// .codex/hooks.json              // same Stop matcher-group shape as Claude
{ "hooks": { "Stop": [ { "hooks": [ { "type": "command", "command": "<resolved> hook" } ] } ] } }
```

OpenCode gets `.opencode/plugins/tycho.js`. OpenCode automatically loads project plugins at startup;
restart an already-running OpenCode process after `tycho init`.

Installer logic lives in `tycho/init.py` (one function per harness — the shapes diverge too much to
share, though Claude and Codex are close enough to share `_install_groups`). Codex loads project hooks
only for trusted projects.

## Is it still working? (`tycho doctor`)

A silently dead hook is the worst failure a verifier has. A wrong verdict argues with you;
a dead hook says nothing, and silence is exactly what "everything passed" looks like.

```sh
tycho doctor      # exits 0 when healthy, 5 when installed-but-not-working
```

It checks, without editing anything: our entry is still in each harness's config; the
command it would run **resolves to a real executable** (the check that matters — the hook
fires in a plain `/bin/sh` with no venv); the install's schema version matches this Tycho;
and when the hook last fired. `tycho verify` runs the config half of this too, and prints
`HOOK BROKEN` / `HOOK OUTDATED` to stderr — the diagnostic has to reach the command people
actually run. Repair is always the same: `tycho init` is self-healing.

**The heartbeat, and its limits.** A dead hook cannot report its own death — it runs no
code. So the hook writes `<repo>/.tycho/last-run.json` on *every* invocation (even when
there's nothing to verify: the question is "did the wiring fire?"), and `doctor` reads it
back. Being honest about what that does and doesn't prove:

- A heartbeat proves the hook fired **at that time**. Proof of life, never proof of
  current health.
- **No heartbeat is not proof of death.** A fresh install hasn't fired yet, and neither
  has a repo you haven't run an agent in — so that's reported as `INFO`, not `BROKEN`. A
  diagnostic that cries wolf gets ignored exactly when it's finally right.
- Nothing polls in the background. A hook that died five minutes ago goes undiagnosed
  until someone asks. That's the trade: Tycho stays a thing that runs when called.

`.tycho/install.json` stamps the schema version (`state.SCHEMA`) and what we wired; bump
it when the installed entry's shape changes and old installs report `HOOK OUTDATED` until
re-run. Every file under `.tycho/` is ours alone, and gitignored.

**The catch record (`tycho count`).** The heartbeat is overwritten every run, so nothing in
it accumulates — `.tycho/catches.json` is what remembers, and a machine-wide twin under
`TYCHO_HOME` sums it across every repo. It holds two things:

- **A running tally** of `FAILED`, `STALE`, and `INDETERMINATE`. "Caught" is the adverse pair
  (`FAILED` + `STALE`); `INDETERMINATE` is tallied too but reported apart, since a blind spot
  isn't a save.
- **An evidence trail** (repo file only) — one entry per adverse/intermediate run, newest
  first, recording the checks that failed or couldn't pass (name, status, evidence). The
  list is bounded to the most recent N; the tally stays exact. The machine-wide twin keeps
  only the tally, not a cross-repo list.

Every adverse/intermediate run is recorded — no transition dedup, so a standing failure
re-reported each turn counts each turn ("hold ALL the failed and intermediate runs"). The
latest verdict is *not* duplicated here; that lives in `last-run.json`. A pre-existing
`counts.json` is migrated on read, then dropped on the next write.

Same fail-open rule as the rest of `state.py`: a record we can't write is simply not
written. A number is never worth breaking someone's Stop over.

## Manual discovery (`tycho verify` with no `--session`)

Run bare, `tycho verify` finds the session to check itself. Claude and Cursor key transcript folders
by the start-time cwd (encoded `/`→`-`, `.`→`-`; Claude keeps the leading dash, Cursor strips it).
Codex stores dated rollout files and records the cwd in `session_meta`:

- Claude → `~/.claude/projects/<enc>/<uuid>.jsonl`
- Cursor → `~/.cursor/projects/<enc>/agent-transcripts/<id>/<id>.jsonl`
- Codex → `~/.codex/sessions/<year>/<month>/<day>/rollout-*.jsonl`

`harness.discover(cwd)` finds the newest `.jsonl` for this repo in each, and picks the **more recent by
file mtime** — i.e. whichever harness you used last. mtime is the only signal all three share (Cursor
transcripts carry no internal timestamps), and it's exactly "most recently used." The chosen harness
also supplies the right `parse` reader, so no extra branching. It prints which harness/session it
picked; `--harness claude|cursor|codex` forces one, and `--session <path>` bypasses discovery entirely.

### Overriding where an agent's data lives

Those `~/.<name>` roots are defaults, not facts — dotfiles get relocated, machines get shared, and
Windows keeps this elsewhere. Every root is overridable. `harness.home(name)` resolves, in order:

| Harness  | Tycho override        | Harness's own var   | Default                   |
|----------|-----------------------|---------------------|---------------------------|
| Claude   | `TYCHO_CLAUDE_HOME`   | `CLAUDE_CONFIG_DIR` | `~/.claude`               |
| Cursor   | `TYCHO_CURSOR_HOME`   | —                   | `~/.cursor`               |
| Codex    | `TYCHO_CODEX_HOME`    | `CODEX_HOME`        | `~/.codex`                |
| OpenCode | `TYCHO_OPENCODE_HOME` | `XDG_DATA_HOME`     | `~/.local/share/opencode` |

Each points at the directory that *directly contains* the harness's data — the one holding
`projects/`, `sessions/`, or `opencode.db`. The harness's own variable is honored before the default so
that relocating the agent relocates Tycho with it, with nothing extra to configure. Empty values are
ignored (treated as unset) and `~` is expanded. OpenCode's chain lives in `opencode.db_path()` rather
than `harness.home()` because its root is an XDG data dir, not a `~/.<name>` dotdir.

Tycho's *own* machine-level root follows the same chain, in `state.user_dir()`: `TYCHO_HOME`, then
`XDG_DATA_HOME/tycho`, then `~/.local/share/tycho`. It holds exactly one thing — the all-time tally
behind `tycho count` — and exists only because that number is the one fact that isn't per-repo.
Everything else Tycho knows lives in `<repo>/.tycho/`, which is where state belongs.

## The transcript schemas are the real difference

Claude, Cursor, and Codex use JSONL; OpenCode stores everything in SQLite (`opencode.db`), which Tycho
reads directly and reshapes into one JSON object.

**Claude Code** transcripts are rich. Each `tool_use` block carries an `id`, a `name`, and `input`;
a matching `tool_result` block (paired by `tool_use_id`) carries the failure signal, and the entry's
`toolUseResult` carries structured output (Bash stdout/stderr; Edit/Write `filePath`/`originalFile`).
Every entry has a top-level ISO `timestamp`. So `events.parse` produces `Event`s with real
timestamps, exit/error signals, and before-content for the AST checks.

**Cursor** transcripts are thin. A `tool_use` block carries only `name` + `input` — **no `id`, no
`timestamp`, and no `tool_result` blocks at all** (no exit codes, no output). Cursor's edit tool is
`Write` with an `input.path` (and `contents`); reads are `Read`, also with `input.path`.

Consequence: running `events.parse` (Claude's reader) on a Cursor transcript returns **zero events** —
it keys on `id` and pairs `tool_result` blocks that don't exist. So Cursor needs its own reader,
`events.parse_cursor`, which pulls each `tool_use` into an `Event(ts=0.0, name, input, is_error=None,
result={})`. `file_edits` reads from `input.file_path`, `input.path`, or `result.filePath` (Claude,
Cursor, or Codex), so the FileEdit projection is shared. It skips events with `is_error` **true** —
a denied or errored call never reached the disk — but keeps `is_error is None`, which
means *no status was recorded* rather than failure; Cursor records none, so dropping those would
leave it with zero edits.

**Codex** rollout files contain `session_meta`, turn-scoped `event_msg` records, and `response_item`
tool calls/results. `events.parse_codex` returns **every** turn's events — extracting shell commands
from `custom_tool_call`, pairing results by `call_id`, and converting successful
`patch_apply_end.changes` entries into file edits — and `events.turn_start_codex` reports the latest
event-bearing turn's `task_started` timestamp. The Stop then narrows to that turn via `turn_start`
exactly as Claude does, while the session-scoped checks keep the full history they need (before,
the reader filtered to the latest `turn_id`, which blinded freshness/provenance to earlier
turns).

Codex documents `transcript_path` as convenient but not stable. Tycho therefore pins the observed
schema with a fixture and must degrade to `UNSUPPORTED` when a future rollout no longer exposes the
required events. A Codex command without a recorded exit status is also `UNSUPPORTED`, never PASS.

**OpenCode** stores each message and tool part as rows in `opencode.db`. Tycho reads the session's
`message` rows (for `role` and the millisecond `time.created` the turn boundary anchors on) and its
`part` rows (each `part.data` is a tool part with `state.input`, millisecond start/end times, status,
output, and `metadata.exit`), and rebuilds the same JSON `events.parse_opencode` consumes, mapping
`bash`, `edit`, and `write` into its shared event vocabulary. If OpenCode omits a shell exit value,
command verification is `UNSUPPORTED`; completion alone is never treated as proof that the command
passed. OpenCode edit parts do carry `oldString`, but Tycho's FileEdit projection doesn't consume it
today, so the two AST-diff checks degrade honestly (a future upgrade could wire `oldString` in).

### Turn scope vs session scope

A Stop verdict is read by a human reviewing **this turn**, but the transcript holds the whole
session. So `Session` carries both: `edits`/`events` are everything, and `turn_edits`/`turn_events`
are the slice at or after `turn_start`. `turn_start` never narrows the underlying tuples — some
checks genuinely need the full history.

| Scope | Checks | Why |
|---|---|---|
| **Turn** | `command_execution`, `file_state`, `git_state`, `scope_drift`, plus the `has_verifiable_activity` gate | They answer "what did this turn do?". Session-scoped, they re-passed on every later Stop once a session had edited anything — a permanent green light unrelated to what just happened. |
| **Session** | `test_freshness`, `test_provenance`, `assertion_weakening`, `skip_mock_injection` | Their whole job is reasoning across turns: a source edited three turns ago and never retested really is stale, and assertion weakening accumulates. |

The turn/session split is a real distinction, not per-check special-casing: the turn-scoped
checks **attribute work** ("did *this turn's* edits land?"), while the session-scoped ones **describe the
tree right now** ("is a source uncovered against the last green run?"). A staleness that's still live but
was caused by an earlier turn is announced as exactly that — `test_freshness` says "still uncovered since
the last passing run (last edited in an earlier turn)" rather than implying this turn caused it, so a
doc-only turn's STALE never reads as an accusation about work you just did.

Where the boundary lives is harness knowledge, so it sits in `harness.py` as `Harness.turn_start`;
the engine only ever sees a float. `0.0` means "the whole transcript is the turn" and is the honest
default — for `tycho verify` (a deliberate whole-session audit), and for any harness that can't mark
turns. The turn-scoped checks word their evidence from the scope they actually ran at ("this turn"
vs "this session"), because mislabelling the scope is the same bug in prose.

| Harness | `turn_start` | Why |
|---|---|---|
| Claude | last user message | A turn is `user prose → assistant work → stop_reason=end_turn`. Anchored on the user side because real sessions emit *adjacent* `end_turn` markers (an empty one, then the contentful one), so counting markers over-counts turns. Pinned by `tests/fixtures/transcript_multiturn.jsonl`. |
| Codex | latest turn's `task_started` | `parse_codex` returns every turn; `turn_start_codex` anchors on the latest event-bearing turn. Codex is the only harness with an explicit `turn_id`, so it can mark the boundary precisely. |
| Cursor | `0.0` | Every Event is `ts=0.0` — no timestamps to scope by — and the transcript is one turn end-to-end. |
| OpenCode | last user message | A user message opens the next turn, so the last one opens the turn the Stop fires on — the same shape snitch reads out of this store. `message.data.time.created` is ms; `turn_start_opencode` scales it to the seconds `parse_opencode` emits. Pinned by `tests/fixtures/opencode_transcript_sample.json` (three real turns). |

### Honest degradation on Cursor

Because Cursor's transcript has no timestamps and no results, the checks that depend on them
**degrade to UNSUPPORTED** for Cursor — they never guess:

| Check | Claude | Cursor (from the Stop transcript) |
|---|---|---|
| `command_execution` | ✅ exit code from `tool_result` | ⚪ UNSUPPORTED — no result recorded |
| `test_freshness` | ✅ timestamps compared | ⚪ UNSUPPORTED — no timestamps |
| `test_provenance` | ✅ | ⚪ UNSUPPORTED — no timings |
| `assertion_weakening` / `skip_mock_injection` | ✅ before-content from transcript | ⚪ UNSUPPORTED — no before-content recorded in transcript |
| `file_state` | ✅ | ✅ works (file exists on disk) |
| `git_state` | ✅ | ✅ works (edits reconciled with git) |
| `scope_drift` | ✅ | ✅ works (edited paths vs config) |

This is the design, not a gap: Tycho says UNSUPPORTED with a reason rather than fake a verdict.
Cursor's *richer* signal (exit codes, per-edit before/after) lives in its **per-tool** hooks
(`afterShellExecution`, `afterFileEdit`), not the Stop transcript — wiring those to full parity is a
later layer, not this hook.

### Honest degradation on Codex

Codex supplies timestamps, command results when the execution result exposes an exit code, and exact
paths/types for successful patch changes. That supports command execution, freshness, provenance,
file state, git state, and scope drift. Its patch event does not currently include the pre-edit file
contents Tycho needs for AST before/after comparison, so `assertion_weakening` and
`skip_mock_injection` report `UNSUPPORTED` for those edits instead of guessing.

## Hook logic (`tycho/hook.py`)

The entrypoint is deliberately small and paranoid — a verifier must never break the agent:

1. Read stdin, `json.loads`. Not JSON, or not an object → **return nothing, exit 0**.
2. `harness.transcript_path(payload)` — falsy (transcripts off, or absent) → **return nothing, exit 0**.
3. `harness.detect(payload)` → the `Harness`.
4. `gather(transcript, harness.repo_root(payload), parse=harness.parse)` → `run_checks` → `verdict_of`.
   Any exception in here (unreadable transcript, git hiccup) → **return nothing, exit 0** (fail open).
5. `harness.format_output(render(verdict, results))` → `print(json.dumps(...))`, **exit 0**.

If the turn contains neither file edits nor a recognized test/build command, the automatic hook emits
nothing. Conversational and read-only turns are not verification failures. Manual `tycho verify`
still renders the full result set.

**Always exit 0. The hook annotates the Stop; it never blocks it** — that's a permanent invariant.
(Contrast `tycho verify`, the manual CLI path, which exits 1 on FAILED so CI can gate.) The verdict
reaches the human through the harness's own message channel, so nothing is fed back into the model
loop.

`hook.run(stdin_text) -> dict | None` is split from stdin/stdout so it's unit-testable without
patching the process; `hook.main()` is the thin I/O wrapper `tycho hook` calls.

### Agent override (`OVERRIDDEN`)

When the relay is on and `[override] enabled = true` (off by default), the agent may record a
per-check override with `tycho override <check> "<reason>"`. The Stop hook drops that check,
recomputes the verdict from the rest, and — only if no adverse (`FAILED`/`STALE`) check survives —
renders the distinct `OVERRIDDEN` verdict (teal; "agent-authorized, not proven"). A surviving
failure keeps its verdict and the relay keeps firing, so an override can never hide a real failure.
Every override is logged to `.tycho/overrides.json`; the marker is cleared on the next user prompt.
Toggle: `tycho override --on|--off` (or `/tycho-override-on` / `-off` in Claude Code).

If the operator disagrees, they veto: `tycho override --veto [<check>]` (or `/tycho-override-veto`)
drops the override and forbids it from being re-applied — with the override gone, the next Stop
recomputes the check as adverse and the relay fires again. A veto persists across turns (it is *not*
cleared on a new prompt) until lifted with `tycho override --unveto <check>`. Vetoes are logged too.

## Where the fixtures come from

Each reader is pinned to a **real** captured transcript, so a schema change breaks a test instead of
silently mis-parsing:

- `tests/fixtures/transcript_sample.jsonl` — Claude Code.
- `tests/fixtures/cursor_transcript_sample.jsonl` — Cursor (a session that read files and wrote a
  markdown doc, so the `Write` edit path is covered).
- `tests/fixtures/codex_transcript_sample.jsonl` — Codex, two turns: turn 1 runs the tests green and
  adds a file, turn 2 edits a source. The tests prove the reader returns both turns, that `turn_start`
  narrows the turn-scoped view to turn 2, and that a source left uncovered since turn 1's green run
  reports STALE.
- `tests/fixtures/opencode_transcript_sample.json` — the tool-part shape captured from OpenCode
  1.17.20's exported session schema, minimized to an edit and a successful test command.

# Harness support matrix

Tycho reads four harnesses it does not control. Each can change its hook contract in a
patch release, and **nothing upstream tells us**. This file is the record of what we
depend on, what version we last confirmed it against, and how to confirm it again.

## Why this file exists

A harness contract that drifts does not raise an error. It produces a *dead check*: the
hook fires, records its heartbeat, returns output the harness ignores — and `tycho doctor`
reports healthy, because doctor proves the hook **fires**, not that the harness
**understood** it. That is the honest boundary of the heartbeat design, and pinned
versions are the only thing that closes it.

This is not hypothetical. Both known instances were found *by accident*:

- **TYCHO-19** — Cursor's Stop output field was `user_message`, inferred and never
  verified. Cursor never reads it. Every Cursor verdict Tycho ever produced was silently
  dropped, for the entire life of the adapter.
- **TYCHO-32** — Claude Code emits `"originalFile": null` on repeat edits, so
  `assertion_weakening` and `skip_mock_injection` — the tamper checks — quietly report
  UNSUPPORTED on real test edits.

Two dead contracts, zero bug reports, zero failing tests. **Our fixtures cannot catch
this**: a fixture pins *our* reading of a payload, so it fails when we regress, never when
the harness moves underneath us. Re-verification has to look at the harness itself.

## The matrix

| | **Claude Code** | **Cursor** | **Codex** | **OpenCode** |
|---|---|---|---|---|
| Verified against | 2.1.210 | cursor-agent 2026.07.09-a3815c0 | codex-cli 0.144.4 | — not installed |
| Confidence | ⚠️ partial | ✅ read from shipped bundle | ⚠️ fixture only | ⚠️ fixture only |
| Completion signal | `Stop` | `stop` | `Stop` | `session.idle` (plugin) |
| Repo root key | `cwd` | `workspace_roots[0]` | `cwd` | `directory` / `cwd` |
| Detect by | (default) | `workspace_roots` / `cursor_version` | `hook_event_name`+`turn_id` | `sessionID` |
| Transcript | `transcript_path` | `transcript_path` | `transcript_path` | `opencode.db` (no file) |
| **Output field** | `systemMessage` | `followup_message` | `systemMessage` | `message` |
| Output reaches | human only | **model** (see below) | human only | TUI toast |
| Failure signal | `is_error` | `is_error` | `_codex_is_error` | n/a |
| Edit baseline | `toolUseResult.originalFile` ⚠️ | — | — | — |
| Runner output | `toolUseResult.{stdout,stderr}` ✅ 2.1.211 | — none kept | — none kept | — none kept |

⚠️ **partial / fixture only** means: the transcript reader is pinned to a real fixture, but
the *output* field is documented-or-inferred, not confirmed against the harness's own code
or a captured live payload. That is precisely the state Cursor was in before TYCHO-19 —
and Cursor turned out to be wrong. **Treat every ⚠️ as unverified, not as probably-fine.**

### Codex hooks on Windows — payload not delivered on stdin (TYCHO-124)

Codex's Stop/SessionStart hooks are **dead on Windows**, verified live against **both**
codex-cli 0.144.4 (stable, npm) and 0.145.0-alpha.18 (desktop). The hook *fires* (`hook: Stop
… Completed`), but Tycho records no heartbeat and no verdict. Root cause: Codex passes the
hook payload on the child process's **stdin** (binary: `write_stdin`, `ChildStdin`), and that
write fails on Windows (binary: `failed to write to stdin`, `Windows stdin`, `(windows sandbox
stdin`). Tycho reads empty stdin, fails open (exit 0, never blocks), and records nothing —
confirmed by piping empty stdin to `tycho.cli hook` (exit 0, no heartbeat, exact match).

The failure is worse than "empty stdin", and **nondeterministic**: a diagnostic hook whose
*first* statement writes a marker file *before* reading stdin never wrote the marker, and the
turn **hung for 2+ minutes** (had to kill a stray `codex.exe`). So on Windows Codex's hook
execution itself is broken at the spawn/pipe level — sometimes the hook runs but gets no
payload (no verdict), sometimes it doesn't run to its first line and hangs the harness. A `.bat`
hook hung too. This is upstream Codex process/pipe handling; **Tycho has no lever** — its hook
code either gets no input or never executes, so there is nothing to fall back to (a cwd-based
discovery fallback can't help a hook that never runs).

**Tycho's own logic is correct** — `tycho init`, discovery (`payload.cwd == str(cwd)`), the
reader, `tycho verify`, and the `~/.codex` data root all work on Windows; `detect()` returns
`codex` correctly. Only the hook-*fires* path is blocked, and the break is upstream Codex. So
on Windows, Codex is **verify-only** (run `tycho verify --harness codex` manually); the automatic
Stop verdict is **unsupported pending an upstream Codex fix**. Re-verify when Codex ships a
Windows hook fix; the `systemMessage` render-path (TYCHO-111) can't be captured until then.

### Cursor's output is not like the others

Cursor's stop hook reads exactly one key, `followup_message`, and replays it as a
`UserMessageAction` — into the model loop. There is **no human-only channel**. So
`_cursor_output` appends a relay instruction ("show verbatim, end your turn") to keep
Tycho a reporter rather than a commissioner of work. That is a prompt, not a guarantee;
`loop_count`/`loop_limit` is Cursor's own hard backstop. Exit 0 still never blocks.

### Claude and Codex verdicts *can* reach the model — the opt-in relay (TYCHO-35)

The matrix row says Claude output reaches "human only", and by default it does: `systemMessage`
renders to the terminal and never enters the model's context (confirmed both ways in TYCHO-35 — the
human saw the verdict, the model demonstrably didn't). But that is a property of the *field we choose*,
not a hard limit of the harness.

**Verified against Claude Code 2.1.212** (method 1 — read from the shipped binary; it's a compiled
Mach-O, so `grep -a -b -o -F <string>` for byte offsets then decode the surrounding bytes, as the
TYCHO-35 ticket describes). Two Stop-adjacent channels *do* reach the model:

| Channel | What the binary does with it | Cost |
|---|---|---|
| **Stop** `hookSpecificOutput.additionalContext` | injected as a `hook_additional_context` message **and the turn continues** (`preventContinuation` stays false — it is *not* `decision:"block"`) | one extra generation per injection |
| **UserPromptSubmit** `hookSpecificOutput.additionalContext` | injected as a `hook_additional_context` message on the user's **next** prompt | none — rides the user's turn |

Neither is `decision:"block"`, so the **never-blocks invariant holds**: the hook still exits 0 and
nothing is halted. The Stop channel's continuation is what the **verdict relay** uses to make the agent
keep working until `VERIFIED` — deliberately **off by default** (no context spent unless opted in) and
**bounded** (`state.relay_streak` caps auto-continuations per user turn at `relay_max()`, default 3), so
an unsatisfiable verdict converges on a hard stop rather than an infinite loop. See `hook._relay_output`
and the README's relay section. This revises TYCHO-35's original constraint 3 ("no extra generation") per
an explicit operator decision: the relay *does* spend generations, on purpose, only when the user turns it
on.

**Codex 0.144.5** exposes the equivalent continuation through its Stop output schema:
`{"decision":"block","reason":"..."}` rejects the attempted stop and uses `reason` as the
continuation prompt. Tycho sends the adverse-only report there and keeps the full report in
`systemMessage`, sharing the same opt-in flag and bounded streak as Claude. This is supported on
macOS/Linux; the upstream Windows hook stdin/spawn failure described above still prevents it there.

### Captured runner output (Claude Code only) — TYCHO-60

When the shell masks a runner's exit status (`pytest; echo done`, `pytest | tail`), the
runner's own summary line is the only evidence left, so `checks._captured_output` reads
`toolUseResult.stdout`/`stderr` and `runlog.outcome` looks for the verdict in it. That is a
second Claude-only contract, so it gets a row and this note.

**Verified against 2.1.211**, method 2 — 2356 real Bash payloads captured across every
session on the author's machine, which is a far wider net than one live capture:

| | |
|---|---|
| Keys (2281/2356) | `stdout`, `stderr`, `interrupted`, `isImage`, `noOutputExpected` |
| Also seen | `gitOperation`, `returnCodeInterpretation`, `backgroundTaskId`, `persistedOutputPath`/`Size`, `timedOutAfterMs` |
| Exit code | **none** — there is no numeric status; `is_error` is the only failure signal |
| **stdout cap** | **30000 chars, head kept, no truncation marker** |

**The cap is the ceiling on this whole fallback, and it cuts exactly the wrong end.**
pytest prints its summary *last*, so on any run longer than 30k of output the verdict is
the first thing discarded — and nothing in the payload says so. That is why
`_captured_output` reads only the **tail** (`_SUMMARY_TAIL_LINES`): on a truncated capture
the tail is mid-run noise, no summary matches, and the check honestly reports UNSUPPORTED.
Scanning the whole blob instead would let a stray "5 passed" from the middle of a run that
ended red read as green — trading a blind spot for a fabricated one.

The other three harnesses record **no runner output at all** (`parse_cursor` and
`parse_codex` never set `result`; `parse_opencode` sets a synthetic `{"type": "edit"}`),
so for them a masked status stays UNSUPPORTED and there is nothing to re-verify. Codex is
the exception that proves the rule: it never had an exit code, so it has always read the
summary out of the output text — that reading is now `runlog`, shared with `checks`.

Two consequences worth keeping in mind:

- **`is_error is None` implies `result == {}`.** They are written on the same line
  (`events.py`): no `tool_result` means neither a status nor output. So "no exit status
  recorded" is *not* recoverable from output — that blind spot is structural, not a gap
  in the parsing, and the eval pins it as a permanent miss rather than a TODO.
- **A recovered verdict is weaker evidence than an exit code**, so `command_execution`
  says which one it used ("read from its output — exit status masked by the shell").
  A reader who can't tell the difference can't weigh it.

To re-verify:

```sh
python3 - <<'PY'
import json, glob
lens, keys = [], set()
for f in glob.glob(f"{__import__('os').path.expanduser('~')}/.claude/projects/**/*.jsonl", recursive=True):
    for line in open(f, errors="ignore"):
        try: tur = json.loads(line).get("toolUseResult")
        except Exception: continue
        if isinstance(tur, dict) and "stdout" in tur:
            keys.add(tuple(sorted(tur))); lens.append(len(tur["stdout"] or ""))
print("max stdout:", max(lens), "| shapes:", len(keys))   # cap shows up as a flat max
PY
```

## The status bar — auto-wired on Claude Code, renderable anywhere

Two claims that are easy to conflate, and only the second is Claude-only (TYCHO-46):

- **The command is harness-agnostic.** `tycho statusline` reads `.tycho/` off disk, imports no
  engine, and falls back to the cwd when there's no JSON on stdin (`status.repo_of`) — so a
  shell prompt, tmux, or starship renders the same badge on any of the four. Nothing below
  applies to that path; there's no harness in it. (It walks up from the cwd for `.tycho/`,
  stopping at the git root, so the badge survives a subdirectory — TYCHO-79.)
- **The auto-wiring is Claude-only.** `statusLine` is the only "run this command, render its
  stdout" setting among the four, so `tycho init` writes the badge there and nowhere else
  (`init._with_statusline`). Cursor, Codex, and OpenCode have hook surfaces but no slot to
  write into — a missing setting upstream, not a missing feature here.

That wiring is a second contract with the same harness, so it gets the same treatment
(TYCHO-39).

**Verified against 2.1.210**, method 1 — read out of the shipped binary
(`node_modules/@anthropic-ai/claude-code/bin/claude.exe`: the JS is bundled into a Mach-O,
so `strings` it and grep). Confidence ✅ — the settings schema came from the zod
declaration and the execution rules from the call site, both verbatim:

| | |
|---|---|
| Settings key | `statusLine: {type: "command", command: str, padding?: int, refreshInterval?: int (min 1)}` |
| Payload | JSON on **stdin** — `cwd`, `workspace.{current_dir,project_dir}`, `session_id`, `transcript_path`, `model`, `version`, `cost`, … |
| Repo root key | `workspace.project_dir` (`cwd` follows the user into subdirs) |
| Output read | **stdout, only on exit 0** — trimmed, blank lines dropped, multi-line joined |
| Colour | ANSI supported, rendered dimmed |
| Timeout | ~5s (`AbortSignal.timeout(5000)`), then aborted |
| Cadence | event-driven; `refreshInterval` adds polling on top |
| Disabled by | `disableAllHooks: true`, or workspace trust not accepted |

Two consequences worth keeping in mind:

- **Exit 0 or it's dead.** A non-zero exit makes the harness ignore stdout entirely — the
  same class of silent nothing as a dead output field. `tycho statusline` therefore catches
  everything and returns 0 with empty output, which renders as nothing.
- **Windows: the command runs through Git Bash**, which consumes unquoted backslashes;
  the harness's own guidance is to write paths with forward slashes. Our commands come
  from `sys.executable`/`shutil.which`, so they are backslashed on Windows — TYCHO-43.

To re-verify:

```sh
B=/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
strings -n 6 $B > /tmp/cc.txt
grep -o 'statusLine:v.object([^)]*)' /tmp/cc.txt                # the settings schema
grep -o 'async function Mws(e,t,r=5000.\{0,900\}' /tmp/cc.txt   # spawn + stdout rules
```

## The bootup update-notice — SessionStart (TYCHO-53, TYCHO-72)

At agent bootup Tycho surfaces a *newer-version-available* notice (`hook.session_start()` →
`version.notice()`). The hard rule (TYCHO-35): it must land on a **user-facing** channel and
never a model-facing one — a model that reads "a new Tycho is out" could go try to update
itself. So this is a third contract per harness: does a session-start hook exist, and does it
have a **human-only** sink?

| | **Claude Code** | **Cursor** | **Codex** | **OpenCode** |
|---|---|---|---|---|
| Verified against | 2.1.212 | cursor-agent 2026.07.09-a3815c0 | codex-cli 0.144.4 | opencode 1.17.20 |
| Confidence | ✅ read from binary | ✅ read from bundle | ⚠️ schema-parity only | ✅ read from binary |
| Bootup event | `SessionStart` | `sessionStart` | `SessionStart` | `session.created` / `server.connected` (plugin bus) |
| Human-only sink | `systemMessage` ✅ | **none** ❌ | `systemMessage` (inferred) | `client.tui.showToast` ✅ |
| Wire up? | done (`_install_claude`) | **no — no safe channel** | done (`_install_codex`, `hooks.SessionStart`) | done (`_install_opencode`, `session.created`) |

### Claude — verified, and it confirms the existing choice

**2.1.212, method 1.** `SessionStart` is in the hook-event table; hook output is processed
through one shared attachment switch — `hook_system_message`, `hook_additional_context`,
`hook_success`, … — that is *event-agnostic*. So `systemMessage` → `hook_system_message`
(the user-UI attachment, the same one TYCHO-35 proved reaches the human and not the model),
while SessionStart's own `additionalContext` → `hook_additional_context` (model-facing). The
embedded hook docs say it in words: *"systemMessage — Display a message to the user (all
hooks)."* Tycho's `systemMessage` notice is correct. Re-verify:

```sh
B=/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
grep -a -b -o -F 'hook_system_message' $B    # shared, event-agnostic user-UI attachment
grep -a -o 'hook_blocking_error.\{0,120\}'  $B | head   # …lists it beside hook_additional_context
```

### Cursor — the event exists, the channel does not

**cursor-agent 2026.07.09, method 1** (JS bundle). `sessionStart` is a real event (maps to
`SessionStart` in `index.js`), but its output validator honors only `env`,
`additional_context`, `continue`, and `user_message`. There is **no `systemMessage`
analogue** — `additional_context` → `additionalContext` and `user_message` → `userMessage`
both re-enter the model loop (same `userMessage`/`agentMessage`/`additionalContext` triad as
every other Cursor hook; see the stop-hook note above — Cursor has no human-only channel
anywhere). A bootup notice here would necessarily be model-facing, which the TYCHO-35 rule
forbids. **Not supported.** Re-verify:

```sh
D=~/.local/share/cursor-agent/versions/<version>
grep -o '\[r.sessionStart\]:e=>{.\{0,700\}' $D/index.js   # output validator: env/additional_context/continue/user_message, no systemMessage
```

### Codex — schema clone of Claude, render not yet observed

**codex-cli 0.144.4, method 1** (native binary). `SessionStart` is in `HookEventNameWire` /
`ManagedHooksRequirements`, and the output wire format is a near-exact clone of Claude's:
`SessionStartCommandOutputWire` carries `continue`/`stopReason`/`suppressOutput`/
`systemMessage`, `SessionStartHookSpecificOutputWire` carries `additionalContext` (`turn_id`
is flagged a "Codex extension"). So the field to emit is `systemMessage`, same as Claude.
**Wired** in `_install_codex`: a `hooks.SessionStart` matcher-group in `.codex/hooks.json`,
installed and stripped alongside Stop in one write. Stays ⚠️ on the *render path*: schema
parity strongly implies `systemMessage` renders human-only, but that isn't observed here —
confirm with a live capture (TYCHO-111) before release, same as Codex's Stop row. Until then
the risk is bounded — a wrong guess means the toast is silent, never model-facing. Re-verify:

```sh
B="$(npm root -g)/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
grep -a -o 'SessionStartCommandOutputWire' $B; grep -a -o 'SessionStartHookSpecificOutputWire' $B
```

### OpenCode — cleanest fit; toast is genuinely user-facing

**opencode 1.17.20, method 1** (native binary). No hooks file — the plugin bus. Two
session-start-shaped events are defined and delivered to the same `{ event }` callback the
Tycho plugin already uses for `session.idle`: `session.created` (`{sessionID, info}`,
per-session, closest to SessionStart) and `server.connected` (`{}`, once at plugin/server
connect). The user-facing sink is `client.tui.showToast` (`variant: "info"`) — the exact
channel the Stop verdict toast already uses. **Wired** in `_install_opencode`: the plugin's
`session.created` branch runs `tycho session-start` and toasts the notice. One thing not
observable offline — that `session.created` actually reaches plugins (the running harness
isn't installed to capture it); if it doesn't fire, the notice is simply silent, never wrong.
Re-verify:

```sh
B=~/.opencode/bin/opencode
grep -a -o '.\{40\}type:"session.created".\{80\}' $B   # bus event def: {sessionID, info}
grep -a -o 'type:"server.connected"' $B
```

## How to re-verify

The procedure that actually worked for Cursor (TYCHO-19), in order of preference:

1. **Read the harness's shipped code.** Strongest evidence available offline, and
   exhaustive over the contract — it covers fields a single live capture would leave
   unpopulated. For Cursor the agent is a JS bundle:

   ```sh
   D=~/.local/share/cursor-agent/versions/<version>
   grep -rl "hooks.json\|beforeShellExecution" $D/*.js     # find the hooks engine
   grep -o '.\{400\}_E\.stop.\{400\}' $D/*.index.js        # payload assembled at the call site
   grep -o '.\{100\}followup_message.\{300\}' $D/index.js  # output validator + what consumes it
   ```

   Look for two things, separately: what the payload **carries in**, and what the output
   validator **reads back**. The second is where dead fields hide.

2. **Capture a live payload.** Install a stop hook that dumps stdin in a throwaway repo,
   run the harness non-interactively. Confirms reality but only covers one code path, and
   for Cursor needs `--trust`, which launches a third-party agent loop — get explicit
   approval first.

3. **Vendor docs.** Weakest. Docs are what the contract was meant to be; `user_message`
   was plausible enough to ship.

Then: pin what you found in `tests/fixtures/<harness>_stop_payload.json`, assert the
**output key** in `tests/test_m4.py`, and update the version + confidence in the matrix
above. Field *names* are the contract — fixture values stay synthetic.

## Cadence

Calendar reviews rot; nobody reads a reminder to audit four harnesses. So the triggers are
tied to things that already happen:

- **Before each release** — re-verify every ⚠️ row. Non-negotiable: shipping a dead
  output field is shipping a product that silently does nothing.
- **On a version change** — when an installed harness moves off the pinned version,
  re-verify that harness's output field. TYCHO-34 makes `tycho doctor` surface this, so
  drift shows up where someone is already looking instead of relying on memory.
- **On any "the verdict never appeared" report** — treat as a dead-hook contract bug until
  proven otherwise. That symptom has been right both times.

Re-verifying one harness is ~15 minutes with the grep recipe above. The expensive thing is
not doing it.

## Adding a harness

A new harness is one `Harness` entry + (if its transcript schema is new) one `parse_*`
reader + one pinned fixture — no engine change (see `docs/hooks.md`). It is **not done**
until it has a row here with a verified output field. An adapter whose output field is
guessed is an adapter that reports to nobody.

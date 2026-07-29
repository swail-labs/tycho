"""`verifier_integrity` — did this turn edit the verifier that is judging it?

Every other check asks whether the agent's claim is backed. This one asks whether the
*record* is. Tycho's whole position is that it is independent of its subject, and until this
existed the subject could empty `[scope]`, add a check to `[checks].disable`, truncate
`.tycho/turns.jsonl`, or lift the Stop hook out of `.claude/settings.json` — and no check
looked. A verifier the auditee can edit is the auditee's diary.

Two channels, because an agent has two ways to write a file:

  * the edit tools, which land in `session.turn_edits` — unambiguous, and the whole rule is a
    path test;
  * the shell, where the path is a token in a command string. Here it takes a *mutating* verb
    (or a redirect onto the path) to count, so reading the record is not an accusation.

`tycho` itself is exempt: `tycho scope add`, `tycho relay --off` and `tycho override` are the
sanctioned interface, they are what the slash commands run, and override already logs itself
to `.tycho/overrides.json`. The bound this check defends is "not behind Tycho's back", not
"never".

**Not disableable.** `run_checks` subtracts this one from `[checks].disable`, because a check
whose subject can switch it off is the hole it was written to close.
"""

from __future__ import annotations

import shlex

from ...model import CheckResult, CheckStatus, Session
from .cmdread import _MAX_CMD_LEN, _SEGMENT_SEP, _SHELL_TOOLS, _strip_heredocs, written_paths
from .common import _is_in_repo, _r, _scope

NAME = "verifier_integrity"

# Hook wiring, protected wherever it lives — including outside the repo. `tycho init --global`
# writes the user-level `~/.claude/settings.json`, so the file that makes Tycho run for this
# turn may legitimately sit in `$HOME`, and bounding these to the repo would leave the global
# install undefended. Suffix-matched: `/Users/x/.claude/settings.json` is that file.
_HOOK_CONFIG_TAILS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".cursor/hooks.json",
    ".codex/hooks.json",
    ".opencode/plugins/tycho.js",
)

# Tycho's own state and config. Repo-bounded, unlike the above: `.tycho/` under some other
# tree is another project's ledger or a scratch directory, and neither is this verdict's
# business. Reported on this repo's own turn, an out-of-repo write is `scope_drift`'s to
# flag — the same bound `_is_source_path` draws, and for the same reason it had to.
_STATE_DIR = ".tycho"
_CONFIG_NAME = ".tycho.toml"

# Where the `prepare-commit-msg` trailer hook lives. A directory rather than a filename: a
# `core.hooksPath` inside the repo puts it somewhere else entirely, and every git hook in a
# repo Tycho wired is wiring an agent has no business rewriting mid-turn.
_HOOK_DIRS = (".git/hooks/", ".githooks/")

# Verbs that change a file rather than read one. Deliberately short: every one of these is a
# command whose whole job is to write, move or destroy, so naming a protected path in one is
# never incidental. ponytail: a verb list, not a syscall trace — an agent that writes through
# `python -c "open('.tycho.toml','w')"` is caught by nothing here. Ceiling stated in the
# module docstring's threat model; the edit-tool channel is the one that closes cheaply.
_MUTATING = frozenset({
    "rm", "rmdir", "unlink", "mv", "cp", "install", "ln", "truncate", "shred", "dd",
    "sed", "perl", "tee", "chmod", "chown", "touch", "patch", "mkdir",
})


def _normalize(path: str) -> str:
    return str(path or "").replace("\\", "/").removeprefix("./")


def _is_protected(path: str) -> bool:
    """True for a file that is Tycho, or the wiring that makes Tycho run.

    Hook configs count wherever they are (`--global` puts one in `$HOME`); Tycho's own state
    and the git hooks count only inside this repo. Caught on Tycho's own turn: a smoke test
    ran `mkdir -p "$SCRATCH/repo/.tycho"` against a throwaway tree under `/tmp`, and a check
    that matched `.tycho` in any path called that tampering with the record it was writing.
    """
    p = _normalize(path)
    if not p:
        return False
    # An unexpanded variable is not a path. `written_paths` already drops `> $OUT` for this
    # reason; a token like `$SCRATCH/repo/.tycho` names a directory only the shell knows.
    if "$" in p:
        return False
    if any(p == tail or p.endswith("/" + tail) for tail in _HOOK_CONFIG_TAILS):
        return True
    if not _is_in_repo(p):
        return False  # another tree's Tycho, or a scratch dir — not this verdict's business
    if _STATE_DIR in p.split("/") or p == _CONFIG_NAME or p.endswith("/" + _CONFIG_NAME):
        return True
    return any(p.startswith(d) or f"/{d}" in p for d in _HOOK_DIRS)


def _tokens(cmd: str) -> list[str]:
    """The command's words. `shlex` when it parses, a plain split when it doesn't — an
    unparseable command is still a command, and dropping it is how a check goes quiet."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _shell_hits(session: Session) -> list[str]:
    """Protected paths this turn's shell commands wrote, as `path (command)` evidence.

    A redirect onto the path counts on its own — `echo '' > .tycho.toml` names no verb. Any
    other spelling needs a mutating verb in the same command, so `cat .tycho/turns.jsonl` and
    `grep -r foo .tycho/` stay clean.
    """
    hits = []
    for event in session.turn_events:
        if event.tool not in _SHELL_TOOLS:
            continue
        cmd = event.input.get("command") or ""
        if not cmd or len(cmd) > _MAX_CMD_LEN:
            continue
        for path in _protected_writes(cmd):
            hits.append(f"{path} (shell)")
    return hits


def _protected_writes(cmd: str) -> list[str]:
    """Protected paths this one command writes."""
    found = [p for p in written_paths(cmd) if _is_protected(p)]
    # Per segment, so `cat .tycho/turns.jsonl && rm scratch.txt` doesn't read as a deletion
    # of the record: the verb and the path have to be in the same command.
    for segment in _SEGMENT_SEP.split(_strip_heredocs(cmd)):
        if _mutates(segment):
            found += [t for t in _tokens(segment) if _is_protected(t)]
    return sorted(dict.fromkeys(found))


def _mutates(segment: str) -> bool:
    """Does this segment run a verb that writes? A `tycho` segment never does — the CLI is
    the sanctioned way to change Tycho's own settings, and `tycho override` already writes
    its own audit trail."""
    words = _tokens(segment)
    if not words:
        return False
    # `VAR=1 rm …` — an env assignment prefix, same idiom `_normalize_segment` strips.
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]
    if not words:
        return False
    exe = words[0].rsplit("/", 1)[-1]
    if exe.startswith("tycho"):
        return False
    if exe in _MUTATING:
        return True
    # `env FOO=1 rm …`, `sudo rm …`, `xargs rm` hide the verb further in.
    return exe in ("env", "sudo", "xargs", "command") and any(w in _MUTATING for w in words[1:])


def verifier_integrity(session: Session) -> CheckResult:
    edits = sorted({fe.path for fe in session.turn_edits if _is_protected(fe.path)})
    shell = _shell_hits(session)
    if edits or shell:
        return _r(
            NAME,
            CheckStatus.FAIL,
            "this turn edited Tycho's own configuration or record: "
            + ", ".join([*edits, *shell]),
        )
    looked_at = bool(session.turn_edits) or any(
        e.tool in _SHELL_TOOLS for e in session.turn_events
    )
    if not looked_at:
        return _r(
            NAME,
            CheckStatus.UNSUPPORTED,
            f"no files edited and no commands run this {_scope(session)}",
        )
    return _r(NAME, CheckStatus.PASS, "Tycho's own config, record and hooks untouched")

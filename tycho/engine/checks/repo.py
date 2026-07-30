"""`file_state`, `git_state`, `scope_drift` — do the claimed edits exist, does git agree,
and did they stay inside the declared scope?"""

from __future__ import annotations

from fnmatch import fnmatchcase

from ...model import CheckResult, CheckStatus, Session
from .common import _is_in_repo, _r, _scope


def file_state(session: Session) -> CheckResult:
    paths = {fe.path for fe in session.turn_edits}
    if not paths:
        return _r(
            "file_state", CheckStatus.UNSUPPORTED, f"no files created or edited this {_scope(session)}"
        )
    broken = []
    for path in sorted(paths):
        fs = session.files.get(path)
        if fs is None or not fs.exists:
            broken.append(f"{path} — missing")
        elif not (fs.current_text or "").strip():
            broken.append(f"{path} — empty")
    if broken:
        return _r("file_state", CheckStatus.FAIL, "; ".join(broken))
    return _r(
        "file_state",
        CheckStatus.PASS,
        f"all {len(paths)} file(s) edited this {_scope(session)} present on disk",
    )


def git_state(session: Session) -> CheckResult:
    if not session.git.is_repo:
        return _r("git_state", CheckStatus.UNSUPPORTED, "not a git repository")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "git_state",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to reconcile against git",
        )
    # An out-of-repo edit judged against this repo's diff reads as "reconciled" on file_state's
    # evidence (it exists on disk), not git's.
    in_repo = {p for p in edited if _is_in_repo(p)}
    outside = edited - in_repo
    if not in_repo:
        return _r(
            "git_state",
            CheckStatus.UNSUPPORTED,
            f"{len(outside)} path(s) edited outside the repo — nothing here for git to reconcile",
        )
    changed = set(session.git.changed_paths)
    # A phantom is an edit that's neither in the working diff nor on disk.
    phantom = [
        p for p in sorted(in_repo)
        if p not in changed and not (session.files.get(p) and session.files[p].exists)
    ]
    if phantom:
        return _r("git_state", CheckStatus.FAIL, f"claimed edits absent from repo: {', '.join(phantom)}")
    evidence = (
        f"{len(in_repo)} path(s) edited this {_scope(session)} reconciled with git "
        f"({len(in_repo & changed)} uncommitted)"
    )
    if outside:
        evidence += f"; {len(outside)} outside the repo — not tracked here"
    return _r("git_state", CheckStatus.PASS, evidence)


def scope_drift(session: Session) -> CheckResult:
    globs = session.config.scope_include
    excludes = session.config.scope_exclude
    if not globs:
        # A concrete glob, not a `<glob>` metavariable: Codex HTML-escapes the field it delivers
        # a verdict on, so angle brackets arrive as `&lt;glob&gt;` — in the copy the model reads,
        # and in a command we are telling someone to run.
        return _r("scope_drift", CheckStatus.UNSUPPORTED,
                  "no [scope] set — run `tycho scope add 'src/**'` to bound edits (zero-config)")
    edited = {fe.path for fe in session.turn_edits}
    if not edited:
        return _r(
            "scope_drift",
            CheckStatus.UNSUPPORTED,
            f"no edits this {_scope(session)} to check scope against",
        )
    def _in_scope(p: str) -> bool:
        return any(fnmatchcase(p, g) for g in globs) and not any(fnmatchcase(p, g) for g in excludes)

    outside = [p for p in sorted(edited) if not _in_scope(p)]
    if outside:
        return _r("scope_drift", CheckStatus.FAIL, f"edits outside stated scope: {', '.join(outside)}")
    within = f"all edits within scope {list(globs)}"
    return _r("scope_drift", CheckStatus.PASS, f"{within} (excluding {list(excludes)})" if excludes else within)

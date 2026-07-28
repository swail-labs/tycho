"""Reading back what the record already holds — no engine, no verification.

`show` / `blame` / `log` / `review` / `attest` render turns; `count` renders the running tally
and the decay ledger.
"""

from __future__ import annotations

from pathlib import Path

from . import ExitCode

def _show(cwd: Path, turn: str | None) -> int:
    """`tycho show [TURN]` — the full digest of a turn, on demand."""
    from ..views import digest as digest_mod
    from ..store import record as record_mod
    from ..store import state

    repo = state.root_for(cwd)
    records = record_mod.read(repo, limit=1) if not turn else [
        r for r in record_mod.read(repo) if str(r.get("id") or "").startswith(turn)
    ]
    if not records:
        print("tycho: no turn recorded yet — the Stop hook writes one per verified turn.")
        return ExitCode.OK
    print(digest_mod.render(records[0]))
    return ExitCode.OK


def _archaeology(action: str, cwd: Path, target: str | None, limit: int,
                 verdict: str | None = None, since: str | None = None) -> int:
    """`tycho blame <path>` / `tycho log` — what the agent did here."""
    from ..views import archaeology
    from ..store import state

    repo = state.root_for(cwd)
    lines = (
        archaeology.blame(repo, target, limit) if action == "blame"
        else archaeology.log(repo, limit, verdict=verdict, since=since)
    )
    for line in lines:
        print(line)
    return ExitCode.OK


def _review(cwd: Path, since: str, exit_code: bool = False) -> int:
    """`tycho review` — which changes nothing exercised. Advisory unless `--exit-code`."""
    from ..views import review as review_mod
    from ..store import state

    lines, findings = review_mod.inspect(state.root_for(cwd), since)
    for line in lines:
        print(line)
    if not exit_code:
        return ExitCode.OK
    return ExitCode.UNEXERCISED if review_mod.unexercised(findings) else ExitCode.OK


def _attest(cwd: Path, verify: str | None = None, write: list[str] | None = None,
            require_verified: bool = False) -> int:
    """`tycho attest [--verify REF | --write MSGFILE [SOURCE]]`. Bare: print the trailer for
    what's staged. `--verify` exits MISMATCH only on a genuine mismatch, never on "cannot tell" —
    a pruned record must not read as a forged one. `--write` can never fail a commit."""
    from ..wire import attest as attest_mod
    from ..store import state

    repo = state.root_for(cwd)
    if write:
        return attest_mod.main(write)
    if verify:
        ok, text = attest_mod.verify(repo, verify, require_verified=require_verified)
        print(text)
        return ExitCode.MISMATCH if ok is False else ExitCode.OK
    line = attest_mod.trailer(repo)
    if line is None:
        print("tycho: nothing staged that a recorded turn touched — nothing to attest.")
        return ExitCode.OK
    print(line)
    return ExitCode.OK


def _count(cwd: Path, show_ledger: bool = False) -> int:
    """`tycho count [--ledger]` — the running tally of what Tycho caught, read straight off what
    the hook wrote (no engine, no verification). "Caught" is FAILED + STALE; INDETERMINATE folds
    into *blind*, because a blind spot isn't a save."""
    from ..store import state

    totals = state.totals(cwd)
    here = _caught(state.counts(cwd), totals)
    everywhere = _caught(state.all_time_counts(), state.all_time_totals())
    print(f"this repo: {here} · all-time: {everywhere}")
    if show_ledger:
        print()
        # `ledger` resolves the root itself; `totals` is passed in so the view can explain
        # its denominator against the tally printed above it.
        for line in _ledger_lines(state.ledger(cwd), repo_runs=totals.get("runs")):
            print(line)
    return ExitCode.OK


def _caught(counts: dict, totals: dict) -> str:
    """"274 runs, 41 blind (15%), 12 caught (9 FAILED, 3 STALE)". Blind rate leads and shows
    even at 0%: it's the one number that doesn't improve as models get better."""
    caught = counts["FAILED"] + counts["STALE"]
    breakdown = ", ".join(f"{counts[v]} {v}" for v in ("FAILED", "STALE") if counts[v])
    text = f"{caught} caught ({breakdown})" if caught else "0 caught"
    runs = totals["runs"]
    if not runs:  # legacy tally with no denominator, or a genuinely quiet repo
        return text
    blind = totals["blind"]  # INDETERMINATE + UNSUPPORTED — runs Tycho couldn't speak to
    return (f"{runs} run{'' if runs == 1 else 's'}, "
            f"{blind} blind ({round(100 * blind / runs)}%), {text}")


def _pct(n: int, denominator: int) -> str:
    """"25%", or "—" when the denominator is zero — an empty denominator is not 0%."""
    return f"{round(100 * n / denominator)}%" if denominator else "—"


# Below this, a rate is an artefact of the sample, not a property of the check: 0 of 3 renders
# as a confident 0% and reads as "this check is dead".
_MIN_SAMPLE = 10


def _rate(n: int, denominator: int) -> str:
    """"8 (80%)", or a bare "1/3" when the sample is too thin for a percentage to mean
    anything. Zero denominator keeps the "—": nothing was ever asked."""
    if 0 < denominator < _MIN_SAMPLE:
        return f"{n}/{denominator}"
    return f"{n} ({_pct(n, denominator)})"


def _ledger_lines(data: dict, repo_runs: int | None = None) -> list[str]:
    """Render `state.ledger` — per-model and per-check catch/blind rates. Per check, catch rate
    is over the turns the check could *speak* to, blind rate over every turn it ran in."""
    import time as time_mod

    turns = data["turns"]
    if not turns:
        return ["ledger: no turns recorded here yet — the Stop hook writes one per verified "
                "turn to .tycho/turns.jsonl."]
    span = " ".join(
        time_mod.strftime("%Y-%m-%d", time_mod.localtime(ts))
        for ts in (data["first"], data["last"]) if ts
    ).split()
    when = f", {span[0]} → {span[-1]}" if span else ""
    out = [
        f"ledger: {turns} turn{'' if turns == 1 else 's'} on the record{when}, "
        f"blind {_rate(data['blind'], turns)}, caught {_rate(data['caught'], turns)}",
        "  (the retained turn record — `count` above is the all-time tally)",
    ]
    # `count` and the ledger legitimately differ; name why. Do NOT close the gap by recording a
    # turn from `tycho verify` — it audits a whole session, so it would invent a turn boundary.
    if repo_runs is not None and repo_runs > turns:
        from ..store.record import max_records

        out.append(
            f"  ({repo_runs - turns} of this repo's {repo_runs} runs aren't turns here: "
            f"`tycho verify` audits a whole session, and the record keeps the last "
            f"{max_records()})"
        )
    out.append("")
    width = max([24, *(len(_model_label(m)) + 2 for m in data["models"])])
    out.append(f"  {'model':<{width}}{'turns':>6}  {'caught':<11}{'blind':<11}")
    for m in data["models"]:
        out.append(f"  {_model_label(m):<{width}}{m['turns']:>6}  "
                   f"{_rate(m['caught'], m['turns']):<11}{_rate(m['blind'], m['turns']):<11}")
    if not data["checks"]:
        return [line.rstrip() for line in out]
    cwidth = max([24, *(len(c["name"]) + 2 for c in data["checks"])])
    out += ["", f"  {'check':<{cwidth}}{'spoke':>6}  {'caught':<11}{'blind':<11}by model (caught/spoke)"]
    for c in data["checks"]:
        by_model = ", ".join(
            f"{m['model'] or 'unknown model'} {m['caught']}/{m['spoke']}" for m in c["models"]
        )
        out.append(f"  {c['name']:<{cwidth}}{c['spoke']:>6}  "
                   f"{_rate(c['caught'], c['spoke']):<11}"
                   f"{_rate(c['blind'], c['spoke'] + c['blind']):<11}{by_model}")
    out += ["", "  catch rate = caught / turns the check could speak to (PASS|FAIL|STALE).",
            "  blind rate = blind / every turn it ran in — the metric that doesn't improve "
            "with model capability.",
            f"  rates are shown from {_MIN_SAMPLE} turns up; below that you get the raw "
            f"fraction, because 0/3 is not 0%.",
            "  the retirement signal (§7) is a high blind rate holding across model "
            "generations — never catch rate alone: a check that passed on every turn it "
            "spoke to caught nothing precisely because nothing was wrong."]
    return [line.rstrip() for line in out]


def _model_label(m: dict) -> str:
    """"claude-opus-5 (claude 2.1.220)" — never guessed: an absent id renders as "unknown"."""
    detail = " ".join(str(x) for x in (m.get("harness"), m.get("agent_version")) if x)
    return f"{m.get('model') or 'unknown'}{f' ({detail})' if detail else ''}"

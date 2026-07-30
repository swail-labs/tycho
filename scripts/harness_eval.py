#!/usr/bin/env python3
"""Where does every harness actually stand? Run this while adding one.

    python scripts/harness_eval.py              # the scorecard
    python scripts/harness_eval.py --json       # same, machine-readable
    python scripts/harness_eval.py --update     # re-pin the ratchet floors

The point is a single command you can run repeatedly as a new harness comes up, watching its
row fill in — rather than reading four test files to work out whether "codex is done".

**What the columns mean, and which may go down.**

- `conform` — the contract suite for that harness (detect, payload, output shape, golden
  parse, capability agreement, damaged-input robustness). An invariant: any red fails the run.
- `reach` — how much of the transcript the harness records, out of the declared capabilities.
  Not a score. A 3/8 harness is not worse-built than an 8/8 one, it just keeps less, and no
  amount of work on Tycho moves it. Printed forever, never hidden.
- `catch` — of the lies planted for this harness in `test_eval.py`, how many came back
  adverse. This one *is* a score, and it is floored by the ratchet file.
- `corpus` — captured off the real binary, or authored. Authored means the fixture only
  proves what its author already believed; every reader bug found so far was a shape nobody
  would have invented.
- `pin` — `VERIFIED_AGAINST` against the binary installed here, when there is one to ask.

Exits non-zero on a broken invariant: a conformance failure, a fabricated green, an enabled
harness on an authored corpus, or a catch rate below its pinned floor. Reach never fails.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import pytest  # noqa: E402

from tycho.read import harness as harness_mod  # noqa: E402

SCORECARD = REPO_ROOT / "tests" / "fixtures" / "eval" / "scorecard.json"
SUITES = ("tests/test_harness_conformance.py", "tests/test_eval.py")

# `test_name[claude-truncated_final_line]` -> claude. Bucketing by the parameter id rather
# than a hand-kept list of test names is what makes this survive new tests: anything
# parametrized over the registry lands in the right row for free.
_PARAM = re.compile(r"\[([^\]]+)\]")


class _Collector:
    """Bucket every test outcome by the harness its id names."""

    def __init__(self, names: list[str]):
        self.names = names
        self.passed: dict[str, int] = dict.fromkeys(names, 0)
        self.failed: dict[str, list[str]] = {n: [] for n in names}
        self.global_failures: list[str] = []

    def pytest_runtest_logreport(self, report):
        if report.when != "call" or report.outcome == "skipped":
            return
        match = _PARAM.search(report.nodeid)
        params = match.group(1).split("-") if match else []
        owner = next((n for n in self.names if n in params), None)
        if owner is None:
            if report.outcome == "failed":
                self.global_failures.append(report.nodeid.split("::")[-1])
            return
        if report.outcome == "failed":
            self.failed[owner].append(report.nodeid.split("::")[-1])
        else:
            self.passed[owner] += 1


def _installed_version(name: str) -> str | None:
    pin = harness_mod.VERIFIED_AGAINST.get(name) or {}
    probe = pin.get("probe")
    if not probe:
        return None
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\d+[\w.\-]*", proc.stdout.strip())
    return found.group(0) if found else None


def _run_suites() -> _Collector:
    names = [h.name for h in harness_mod.ALL]
    collector = _Collector(names)
    # pytest's own output would bury the scorecard; outcomes come back via the collector.
    with redirect_stdout(io.StringIO()):
        pytest.main(["-q", "--no-header", "-p", "no:cacheprovider", *SUITES], plugins=[collector])
    return collector


def _eval_rates() -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Per-harness catch counts and any fabricated greens, from the eval corpus.

    Imported after the run so `ACTUAL` is populated — reusing `test_eval`'s own scoring rather
    than restating it, so the two can never disagree about what a catch is.
    """
    import test_eval

    catches: dict[str, tuple[int, int]] = {}
    for scenario in test_eval._READER_LIES:
        if scenario.name not in test_eval.ACTUAL:
            continue
        caught = test_eval.ACTUAL[scenario.name] in test_eval._ADVERSE
        hit, total = catches.get(scenario.harness, (0, 0))
        catches[scenario.harness] = (hit + int(caught), total + 1)

    fabricated = [
        s.name
        for s in test_eval._ALL_LIES
        if s.name in test_eval.ACTUAL
        and test_eval._score(s) is test_eval._Score.FABRICATED_GREEN
    ]
    return catches, fabricated


def build() -> dict:
    import harness_assets as assets

    collector = _run_suites()
    catches, fabricated = _eval_rates()
    capability_count = len(fields(harness_mod.Capabilities))

    rows = {}
    for harness in harness_mod.ALL:
        name = harness.name
        caps = harness.capabilities
        reach = sum(bool(getattr(caps, f.name)) for f in fields(caps))
        meta = assets.capture(name)
        hit, total = catches.get(name, (0, 0))
        rows[name] = {
            "enabled": name in harness_mod.ENABLED_NAMES,
            "conformance_passed": collector.passed[name],
            "conformance_failed": collector.failed[name],
            "reach": reach,
            "reach_of": capability_count,
            "blind": capability_count - reach,
            "catch": hit,
            "catch_of": total,
            "corpus": "captured" if assets.is_captured(name) else "authored",
            "version_captured": meta.get("version_captured"),
            "version_pinned": (harness_mod.VERIFIED_AGAINST.get(name) or {}).get("version"),
            "version_installed": _installed_version(name),
        }
    return {
        "harnesses": rows,
        "fabricated_greens": fabricated,
        "global_failures": collector.global_failures,
    }


def _pin_state(row: dict) -> str:
    pinned, installed = row["version_pinned"], row["version_installed"]
    if not pinned:
        return "unpinned"
    if installed is None:
        return f"{pinned} (no probe)"
    return f"{pinned} ok" if pinned in installed else f"{pinned} STALE, have {installed}"


def render(report: dict, floors: dict) -> list[str]:
    lines = [
        "",
        "tycho harness eval",
        f"  {'harness':10} {'conform':>8}  {'reach':>6}  {'catch':>6}  {'corpus':<10} pin",
    ]
    for name, row in report["harnesses"].items():
        conform = "FAIL" if row["conformance_failed"] else f"{row['conformance_passed']} ok"
        catch = f"{row['catch']}/{row['catch_of']}" if row["catch_of"] else "-"
        flag = "" if row["enabled"] else "  (not enabled)"
        lines.append(
            f"  {name:10} {conform:>8}  {row['reach']:>3}/{row['reach_of']}  {catch:>6}  "
            f"{row['corpus']:<10} {_pin_state(row)}{flag}"
        )
        for failure in row["conformance_failed"]:
            lines.append(f"      FAILED {failure}")

    blind = {n: r["blind"] for n, r in report["harnesses"].items() if r["blind"]}
    if blind:
        lines.append(
            "  structurally blind (never a Tycho bug, never hidden): "
            + ", ".join(f"{n} {c}/{report['harnesses'][n]['reach_of']}" for n, c in blind.items())
        )
    lines.append(f"  fabricated greens: {len(report['fabricated_greens'])} (must be 0)")
    lines.extend(f"      {name}" for name in report["fabricated_greens"])
    lines.extend(f"  FAILED (not harness-specific): {f}" for f in report["global_failures"])
    if floors:
        below = [
            f"{n} {report['harnesses'][n]['catch']} < {floor}"
            for n, floor in floors.items()
            if n in report["harnesses"] and report["harnesses"][n]["catch"] < floor
        ]
        lines.append("  floors: " + (", ".join(below) if below else "all held"))
    return lines


def invariant_failures(report: dict, floors: dict) -> list[str]:
    """Only absolute promises. Reach is never one of them."""
    problems = []
    for name, row in report["harnesses"].items():
        if row["conformance_failed"]:
            problems.append(f"{name}: {len(row['conformance_failed'])} conformance failures")
        if row["enabled"] and row["corpus"] != "captured":
            problems.append(f"{name}: enabled but running on an authored corpus")
        floor = floors.get(name)
        if floor is not None and row["catch"] < floor:
            problems.append(f"{name}: catch {row['catch']} below pinned floor {floor}")
    if report["fabricated_greens"]:
        problems.append(f"fabricated greens: {report['fabricated_greens']}")
    problems.extend(f"failed: {f}" for f in report["global_failures"])
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--update", action="store_true",
        help="re-pin catch floors to today's rates (a deliberate commit, not a default)",
    )
    args = parser.parse_args()

    report = build()
    stored = json.loads(SCORECARD.read_text()) if SCORECARD.exists() else {}
    floors = stored.get("floors", {})

    if args.update:
        SCORECARD.parent.mkdir(parents=True, exist_ok=True)
        floors = {n: r["catch"] for n, r in report["harnesses"].items()}
        SCORECARD.write_text(json.dumps({"floors": floors}, indent=2) + "\n", encoding="utf-8")
        print(f"pinned floors: {floors}")
        return 0

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(render(report, floors)))

    problems = invariant_failures(report, floors)
    if problems:
        print("\nBROKEN INVARIANTS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

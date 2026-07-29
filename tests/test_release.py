"""The release pipeline's own false-green guards.

Plain text assertions, not a YAML parse: the two failure modes here are textual (a `needs`
line goes missing, a guard reverts to `exit 0`) and a parser would cost a dev dependency to
catch the same regressions.
"""

from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
RELEASE = WORKFLOWS / "release.yml"


def test_ci_is_callable():
    """`release.yml` calls CI as a reusable workflow; drop this and the call errors."""
    assert "workflow_call:" in CI.read_text()


@pytest.mark.parametrize("job", ["build", "binaries"])
def test_release_gates_on_ci(job):
    """A tag push doesn't trigger `ci.yml` — only the `needs: [ci]` chain proves the tree was
    green. Without it a red `main` plus a `v*` tag publishes to PyPI."""
    text = RELEASE.read_text()
    assert "uses: ./.github/workflows/ci.yml" in text
    # Scan the job's own body only: every line indented deeper than its `  <job>:` key, up to
    # the next key at that depth. A commented-out `needs` must not count, so match the key at
    # the start of its (stripped) line.
    lines = text.splitlines()
    start = lines.index(f"  {job}:") + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        body.append(line.strip())
    assert any(b.startswith("needs:") and "ci" in b for b in body), (
        f"{job} would run on an unverified tree"
    )


PUBLISH_SH = WORKFLOWS.parent.parent / "packaging" / "homebrew" / "publish.sh"


# The tap's guard lives in the script both workflows call, not in the workflow — same guard,
# one place, so the manual repair path can't publish under rules the release doesn't use.
@pytest.mark.parametrize(
    ("path", "secret"), [(RELEASE, "NODE_AUTH_TOKEN"), (PUBLISH_SH, "TAP_TOKEN")]
)
def test_missing_publish_secret_fails_the_release(path, secret):
    """A publish channel that silently no-ops leaves users on the previous version while the
    release reads green — and `tycho update` nags forever at a version that never shipped."""
    text = path.read_text()
    guard = text.split(f'if [ -z "${{{secret}:-}}" ]; then', 1)[1].split("fi\n", 1)[0]
    assert "exit 1" in guard and "exit 0" not in guard

"""E2E — drive the console script users actually install, not the in-tree package.

Every other test imports ``tycho`` straight from the working tree, which proves nothing
about the artifact people ``pip install``: a module left out of the wheel, a broken
``[project.scripts]`` entry point, or a version silently desynced from pyproject would
all sail through the rest of the suite. So this module builds the artifact, installs it
into a clean venv, and drives the installed ``tycho`` command as a subprocess (TYCHO-9).

Both artifacts are built and both get installed, because ``pip install tycho`` falls back
to the sdist wherever no wheel matches (and always under ``--no-binary :all:``), and an
sdist is assembled by *different* Hatchling rules than a wheel — a ``README.md`` excluded
from the tarball breaks the source install while the wheel stays green. The ``artifact``
fixture parameterizes over the two, so every console-script test below runs against both
(TYCHO-37).

Marked ``e2e``: the build+install costs a couple of seconds per artifact, so it is
session-scoped. Skip with ``-m "not e2e"``. Unlike the rest of the suite these tests need
a network-reachable index *to build* (pip fetches the hatchling backend); installing tycho
itself runs ``--no-index``, which is what proves the zero-dependency claim.
"""

from __future__ import annotations

import email
import json
import os
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

import tycho
from tycho.cli import ExitCode

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def _bin(env: Path, name: str) -> Path:
    """A venv puts scripts in ``bin/`` on POSIX and ``Scripts/*.exe`` on Windows."""
    if os.name == "nt":
        return env / "Scripts" / f"{name}.exe"
    return env / "bin" / name


def _run(*cmd: object, cwd: Path | None = None) -> subprocess.CompletedProcess:
    # Decode as UTF-8, not the platform default: Tycho's report carries emoji/glyphs and
    # its CLI always emits UTF-8, so a cp1252 capture (Windows default) can't decode it.
    return subprocess.run(
        [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the distributable wheel — the artifact itself, unverified until now."""
    dist = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(ROOT)],
        check=True,
        capture_output=True,
    )
    built = list(dist.glob("tycho*.whl"))  # dist is `tycho-cli` → wheel is `tycho_cli-*.whl`
    assert len(built) == 1, f"expected exactly one tycho wheel, built: {built}"
    return built[0]


@pytest.fixture(scope="session")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the source distribution — what `pip install` falls back to without a wheel.

    ``pip`` has no sdist one-liner (the wheel above needs no extra tooling), so this is
    the reason `build` is in the `dev` extra.
    """
    dist = tmp_path_factory.mktemp("dist-sdist")
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "-o", str(dist), str(ROOT)],
        check=True,
        capture_output=True,
    )
    built = list(dist.glob("tycho*.tar.gz"))  # dist is `tycho-cli` → sdist is `tycho_cli-*.tar.gz`
    assert len(built) == 1, f"expected exactly one tycho sdist, built: {built}"
    return built[0]


@pytest.fixture(scope="session", params=("wheel", "sdist"))
def artifact(request: pytest.FixtureRequest) -> Path:
    """Both shipped artifacts, so every console-script test below covers each."""
    return request.getfixturevalue(request.param)


@pytest.fixture(scope="session")
def tycho_cmd(artifact: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install the artifact into a clean venv; yield the ``tycho`` console script it puts there."""
    env = tmp_path_factory.mktemp("cleanenv") / "venv"
    venv.create(env, with_pip=True)
    pip = [str(_bin(env, "python")), "-m", "pip", "install"]
    isolation: list[str] = []
    if artifact.name.endswith(".tar.gz"):
        # Installing an sdist re-runs the build backend on the user's machine — that's the
        # regression surface. Put hatchling in the venv up front so the tycho install can
        # still be --no-index: the point is that *tycho* resolves nothing, not its backend.
        subprocess.run([*pip, "hatchling"], check=True, capture_output=True)
        isolation = ["--no-build-isolation"]
    # --no-index is itself the assertion: tycho declares zero dependencies, so a real
    # install must resolve nothing from the network.
    subprocess.run([*pip, "--no-index", *isolation, str(artifact)], check=True, capture_output=True)
    script = _bin(env, "tycho")
    assert script.exists(), "[project.scripts] installed no `tycho` console script"
    return script


def test_installed_console_script_reports_the_packaged_version(tycho_cmd: Path):
    """Pins entry point + [tool.hatch.version] wiring: the shipped CLI knows its own version."""
    r = _run(tycho_cmd, "--version")

    assert r.returncode == ExitCode.OK
    assert r.stdout.strip() == f"tycho {tycho.__version__}"


def test_wheel_ships_the_package_and_entry_point_but_not_the_tests(wheel: Path):
    names = zipfile.ZipFile(wheel).namelist()

    assert "tycho/cli.py" in names and "tycho/checks.py" in names
    assert not [n for n in names if n.startswith(("tests/", "docs/"))]  # ship the product only
    entry = next(n for n in names if n.endswith("entry_points.txt"))
    assert "tycho = tycho.cli:main" in zipfile.ZipFile(wheel).read(entry).decode()


def test_sdist_ships_the_sources_and_metadata_files_pyproject_points_at(sdist: Path):
    """Hatchling assembles an sdist by different rules than the wheel, so a source file
    that misses the tarball breaks `pip install` from source while the wheel stays green.

    README/LICENSE are asserted for completeness rather than suspicion: Hatchling
    force-includes whatever `readme`/`license` point at (an `exclude` for them is
    ignored), so this pins that behaviour — losing it would ship a tarball PyPI can't
    render. PKG-INFO is the metadata pip and PyPI actually read, hence the version pin.
    """
    with tarfile.open(sdist) as tar:
        names = tar.getnames()
        root = names[0].split("/")[0]
        pkg_info = tar.extractfile(f"{root}/PKG-INFO").read().decode()
    shipped = {n.split("/", 1)[1] for n in names if "/" in n}

    assert "tycho/cli.py" in shipped and "tycho/checks.py" in shipped
    assert "README.md" in shipped and "LICENSE" in shipped
    assert "pyproject.toml" in shipped  # no build backend config, no install from source

    meta = email.message_from_string(pkg_info)
    assert meta["Name"].replace("_", "-").lower() == "tycho-cli"  # distribution name (TYCHO-73)
    # PKG-INFO carries the PEP 440-normalized version (Hatchling normalizes on build, e.g. a
    # prerelease 0.1.0-rc.1 -> 0.1.0rc1), so compare semantically, not by raw string.
    assert Version(meta["Version"]) == Version(tycho.__version__)
    assert meta.get_payload().strip(), 'readme = "README.md" but PKG-INFO carries no description'


def test_installed_cli_verifies_a_real_session_and_honours_the_exit_code_contract(
    tycho_cmd: Path, tmp_path: Path
):
    """The whole point: the *installed* thing reads a transcript and renders a verdict.

    The fixture's edits don't exist in this fresh repo, so the honest verdict is adverse.
    Asserting the exit code *against the rendered verdict* pins the contract CI gates on
    (cli.ExitCode) rather than freezing today's outcome into the test.
    """
    # Arrange: a real git repo — verify reads git state.
    _git(tmp_path, "init")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-m", "init")

    # Act
    r = _run(tycho_cmd, "verify", "--session", FIXTURE, cwd=tmp_path)

    # Assert
    assert "🔍 Tycho:" in r.stdout, f"no rendered report:\n{r.stdout}\n{r.stderr}"
    verdict = r.stdout.split("🔍 Tycho:")[1].split("\n")[0].strip()
    expected = {"FAILED": ExitCode.FAILED, "STALE": ExitCode.STALE}.get(verdict, ExitCode.OK)
    assert r.returncode == expected, f"verdict {verdict} but exit {r.returncode}"


def test_installed_cli_installs_its_hook_into_a_clean_repo(tycho_cmd: Path, tmp_path: Path):
    """`tycho init --yes` is the first thing a user runs after installing."""
    (tmp_path / ".claude").mkdir()

    r = _run(tycho_cmd, "init", "--yes", cwd=tmp_path)

    assert r.returncode == ExitCode.OK, f"init failed:\n{r.stdout}\n{r.stderr}"
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "tycho" in json.dumps(settings["hooks"]["Stop"])

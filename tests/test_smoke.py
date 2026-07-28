"""Phase 0 smoke check: the package imports and reports its version.

The scaffold's one runnable check — fails loudly if the package layout or build config breaks.
Real check coverage lands with the modules in later phases.
"""

import re

import tycho

# X.Y.Z with an optional semver prerelease suffix (e.g. 0.1.0 or 0.1.0-rc.1).
_VERSION_SHAPE = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def test_package_imports_and_has_version():
    # Pin the shape, not the literal — pyproject sources this via [tool.hatch.version],
    # so cutting a release must not require editing a test. See tests/test_cli.py.
    assert _VERSION_SHAPE.fullmatch(tycho.__version__)

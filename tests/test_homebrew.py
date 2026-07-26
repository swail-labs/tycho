"""The Homebrew formula stamper.

Guards the piece that fails silently: a formula pushed to the tap with a wrong or unstamped
sha256 breaks `brew install` for everyone, and nothing upstream of the tap would notice.
"""

import importlib.util
import re
from pathlib import Path

import pytest

_STAMP = Path(__file__).resolve().parents[1] / "packaging" / "homebrew" / "stamp.py"
_spec = importlib.util.spec_from_file_location("tycho_brew_stamp", _STAMP)
stamp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp_mod)

TEMPLATE = (_STAMP.parent / "tycho.rb").read_text(encoding="utf-8")

ARM, INTEL, LINUX, WIN = ("a" * 64), ("b" * 64), ("c" * 64), ("d" * 64)
# sha256sum format as the release job writes it, plus the binary marker and CRLF the npm
# wrapper's parser also tolerates — the two read the same manifest.
SUMS = (
    f"{ARM}  tycho-macos-arm64.tar.gz\n"
    f"{INTEL} *tycho-macos-x86_64.tar.gz\r\n"
    f"{LINUX}  tycho-linux-x86_64.tar.gz\n"
    f"{WIN}  tycho-windows-x86_64.tar.gz\n"
)


def test_stamps_every_marker_from_the_manifest():
    out = stamp_mod.stamp(TEMPLATE, "0.1.0", SUMS)

    assert 'version "0.1.0"' in out
    assert ARM in out and INTEL in out and LINUX in out
    assert "REPLACE_ME" not in out  # a partial stamp must never reach the tap


def test_windows_sum_is_not_stamped_anywhere():
    # Homebrew is macOS/Linux only; the Windows tarball ships for the installer/npm paths.
    assert WIN not in stamp_mod.stamp(TEMPLATE, "0.1.0", SUMS)


def test_missing_asset_raises_instead_of_shipping_a_partial_formula():
    without_arm = "\n".join(ln for ln in SUMS.splitlines() if "macos-arm64" not in ln)
    with pytest.raises(ValueError, match="tycho-macos-arm64.tar.gz"):
        stamp_mod.stamp(TEMPLATE, "0.1.0", without_arm)


def test_tag_form_version_is_rejected():
    # The formula builds its URLs as `.../download/v#{version}/...`; a "v0.1.0" version would
    # request `/download/vv0.1.0/` and 404 on every arch.
    with pytest.raises(ValueError, match="bare semver"):
        stamp_mod.stamp(TEMPLATE, "v0.1.0", SUMS)


def test_prefix_does_not_false_match_another_entry():
    assert stamp_mod.expected_hash(SUMS, "tycho-macos-arm64.tar") is None
    assert stamp_mod.expected_hash(SUMS, "tycho-macos-arm64.tar.gz") == ARM
    assert stamp_mod.expected_hash(SUMS, "tycho-macos-x86_64.tar.gz") == INTEL  # binary marker


def test_caveats_explain_the_npm_collision_without_clobbering_it():
    # The npm wrapper owns <prefix>/bin/tycho when node is brew-installed, so the formula can't
    # link and brew reports a bare "not linked" error. The caveats turn that into instructions.
    # `link_overwrite` would "fix" it by deleting a wrapper the user installed on purpose.
    assert "def caveats" in TEMPLATE
    assert "@swail-labs/tycho" in TEMPLATE
    # The directive itself, not the word — the comment above `caveats` explains why it's absent.
    directives = [ln.strip() for ln in TEMPLATE.splitlines() if not ln.strip().startswith("#")]
    assert not any(ln.startswith("link_overwrite") for ln in directives)


def test_template_and_stamper_agree_on_markers_and_assets():
    # Drift guard: rename a marker or an asset in tycho.rb and the publish job would push an
    # unstamped formula (or stamp the wrong arch) — catch it here instead.
    assert set(re.findall(r"REPLACE_ME_SHA256[A-Z0-9_]*", TEMPLATE)) == set(stamp_mod.ASSETS)
    for asset in stamp_mod.ASSETS.values():
        assert asset in TEMPLATE, f"{asset} is not the tarball any formula URL points at"

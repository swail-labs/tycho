# Homebrew formula for Tycho (TYCHO-69 §2 / TYCHO-105) — the TEMPLATE, not the published file.
#
# It installs the standalone binary produced by the `binaries` release job (see
# docs/bundler-spike.md), so `brew install` pulls no Python toolchain.
#
# The markers below are filled at release time: the `homebrew-publish` job runs
# `packaging/homebrew/stamp.py` against the release's SHA256SUMS and pushes the result to
# `swail-labs/homebrew-tap` as `Formula/tycho.rb`. Edit this file, never the tap copy — the
# next release overwrites it. Renaming a marker or an asset breaks the publish;
# tests/test_homebrew.py fails first if you do.
#
# Then: `brew install swail-labs/tap/tycho`.
# Operator setup (tap repo + token) is in docs/homebrew-remaining.md.
class Tycho < Formula
  desc "Offline, stdlib-only verifier that proves an AI agent did what it claimed"
  homepage "https://swail.dev"
  version "REPLACE_ME_VERSION" # must equal tycho/__init__.py:__version__ for the tagged release
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/swail-labs/tycho/releases/download/v#{version}/tycho-macos-arm64.tar.gz"
      sha256 "REPLACE_ME_SHA256_MACOS_ARM64"
    end
    on_intel do
      url "https://github.com/swail-labs/tycho/releases/download/v#{version}/tycho-macos-x86_64.tar.gz"
      sha256 "REPLACE_ME_SHA256_MACOS_X86_64"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/swail-labs/tycho/releases/download/v#{version}/tycho-linux-x86_64.tar.gz"
      sha256 "REPLACE_ME_SHA256_LINUX_X86_64"
    end
  end

  def install
    bin.install "tycho"
  end

  test do
    assert_match "tycho #{version}", shell_output("#{bin}/tycho --version")
  end
end

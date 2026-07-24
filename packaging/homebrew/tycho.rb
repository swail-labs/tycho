# Homebrew formula for Tycho (TYCHO-69 §2) — TEMPLATE, not yet publishable.
#
# This installs the standalone binary produced by the `binaries` release job (see
# docs/bundler-spike.md), so `brew install` pulls no Python toolchain.
#
# BEFORE THIS WORKS, the operator must (see docs/tycho-69-remaining.md):
#   1. Create the tap repo `swail-labs/homebrew-tap` and drop this file at `Formula/tycho.rb`.
#   2. Cut a GitHub release whose assets include the per-arch binaries, tarred + sha256'd.
#   3. Replace every REPLACE_ME below with the real version, URLs, and sha256 sums.
# Then: `brew install swail-labs/tap/tycho`.
#
# `brew audit --new tycho` and `brew test tycho` must pass before the tap goes public.
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

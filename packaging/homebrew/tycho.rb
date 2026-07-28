# Homebrew formula for Tycho — the TEMPLATE, not the published file.
#
# It installs the standalone binary produced by the `binaries` release job, so
# `brew install` pulls no Python toolchain.
#
# The markers below are filled at release time: the `homebrew-publish` job runs
# `packaging/homebrew/stamp.py` against the release's SHA256SUMS and pushes the result to
# `swail-labs/homebrew-tap` as `Formula/tycho.rb`. Edit this file, never the tap copy — the
# next release overwrites it. Renaming a marker or an asset breaks the publish;
# tests/test_homebrew.py fails first if you do.
#
# Then: `brew install swail-labs/tap/tycho`. Publishing needs the `swail-labs/homebrew-tap`
# repo plus a HOMEBREW_TAP_TOKEN secret; the `homebrew-publish` job fails loudly without it
# rather than shipping a release whose tap stayed stale.
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

  # The npm wrapper (@swail-labs/tycho) installs its shim at
  # `<prefix>/bin/tycho` when node itself is Homebrew-installed — the exact path this formula
  # links. Homebrew then refuses to link (correctly: it never clobbers a file it doesn't own)
  # and reports a bare "Error: ... not linked", which reads like a broken formula rather than
  # two install channels asking for the same name.
  #
  # Both are the same binary and the same release, so there's nothing to reconcile — the user
  # just has to pick an owner. Deliberately NOT `link_overwrite "bin/tycho"`: that would
  # silently delete a wrapper the user installed on purpose.
  def caveats
    return unless (HOMEBREW_PREFIX/"lib/node_modules/@swail-labs/tycho").exist?

    <<~EOS
      The npm wrapper (@swail-labs/tycho) already owns #{HOMEBREW_PREFIX}/bin/tycho, so this
      formula is installed but not linked. Both run the same binary — pick one owner:

        npm uninstall -g @swail-labs/tycho && brew link tycho   # let Homebrew own `tycho`
        brew uninstall tycho                                    # keep the npm wrapper

      `tycho update` upgrades whichever one you keep, via that channel.
    EOS
  end

  test do
    assert_match "tycho #{version}", shell_output("#{bin}/tycho --version")
  end
end

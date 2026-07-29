#!/bin/sh
# Tycho installer — https://swail.dev
#
#   curl -fsSL https://swail.dev/tycho/install.sh | sh
#
# Downloads the standalone Tycho binary for your platform from the GitHub Release, verifies it
# against the release's SHA256SUMS, and installs `tycho` to ~/.local/bin. No Python required;
# needs only curl/wget + tar + sha256sum/shasum (present on macOS/Linux). It is the shell sibling
# of packaging/npm/bin/tycho.js and packaging/homebrew/tycho.rb — same release assets, same
# checksum manifest, one source of truth.
#
# Overrides (env):
#   TYCHO_VERSION       install a specific tag, e.g. v0.1.0 (default: the latest release)
#   TYCHO_INSTALL_DIR   install dir (default: ~/.local/bin)
#
# Everything is wrapped in main() and called at the very end, so a truncated download — the
# classic `curl | sh` hazard — can't execute a half-written script.

set -eu

REPO="swail-labs/tycho"

say() { printf 'tycho: %s\n' "$1" >&2; }
die() { printf 'tycho: error: %s\n' "$1" >&2; exit 1; }

fetch() {
  # fetch <url> <dest>. curl preferred, wget fallback; -f/-q so an HTTP error is a hard failure.
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "need curl or wget to download"
  fi
}

target() {
  os=$(uname -s)
  arch=$(uname -m)
  case "$os" in
    Darwin)
      case "$arch" in
        arm64) echo "tycho-macos-arm64" ;;
        x86_64) echo "tycho-macos-x86_64" ;;
        *) return 1 ;;
      esac ;;
    Linux)
      case "$arch" in
        x86_64 | amd64) echo "tycho-linux-x86_64" ;;
        *) return 1 ;;
      esac ;;
    *) return 1 ;;
  esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "need sha256sum or shasum to verify the download"
  fi
}

main() {
  tgt=$(target) || die "no prebuilt binary for $(uname -s)/$(uname -m). Install with Python instead: pipx install tycho-cli   (or: uv tool install tycho-cli)"
  asset="$tgt.tar.gz"

  if [ -n "${TYCHO_VERSION:-}" ]; then
    base="https://github.com/$REPO/releases/download/$TYCHO_VERSION"
  else
    # GitHub's own redirect to the newest NON-prerelease release's asset — no API, no jq.
    # ponytail: uses /releases/latest; set TYCHO_VERSION to pin a specific or prerelease tag.
    base="https://github.com/$REPO/releases/latest/download"
  fi

  tmp=$(mktemp -d 2>/dev/null || mktemp -d -t tycho)
  trap 'rm -rf "$tmp"' EXIT INT TERM

  say "downloading $asset"
  fetch "$base/$asset" "$tmp/$asset"
  fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS"

  # SHA256SUMS is sha256sum format: "<hash>  <name>" (the "*" binary marker is tolerated too).
  expected=$(grep -E "[[:space:]][*]?$asset\$" "$tmp/SHA256SUMS" | awk '{print $1}' | head -n1)
  [ -n "$expected" ] || die "no checksum for $asset in SHA256SUMS"
  got=$(sha256_of "$tmp/$asset")
  [ "$got" = "$expected" ] || die "checksum mismatch for $asset (expected $expected, got $got) — aborting, nothing installed"
  say "checksum verified"

  tar -xzf "$tmp/$asset" -C "$tmp"
  [ -f "$tmp/tycho" ] || die "archive did not contain a tycho binary"

  dir="${TYCHO_INSTALL_DIR:-$HOME/.local/bin}"
  mkdir -p "$dir"
  mv "$tmp/tycho" "$dir/tycho"
  chmod +x "$dir/tycho"
  say "installed $("$dir/tycho" --version) → $dir/tycho"

  case ":$PATH:" in
    *":$dir:"*) ;;
    *)
      say "note: $dir is not on your PATH — add it, then reopen your shell:"
      printf '\n  export PATH="%s:$PATH"\n\n' "$dir" >&2 ;;
  esac

  setup "$dir/tycho"
}

# Wire Tycho up for every repo, here, rather than telling the reader to go do it. Downloading
# a verifier and then leaving it switched off until someone remembers a second command is how
# a tool ends up installed and never used.
#
# Only with a terminal to answer on: `curl | sh` gives the script no stdin, so the prompt is
# read from /dev/tty when there is one and skipped entirely when there isn't (CI, Dockerfile),
# where printing the command is the honest move.
setup() {
  tycho_bin="$1"
  if [ -t 1 ] && [ -r /dev/tty ]; then
    "$tycho_bin" install < /dev/tty && return 0
    say "setup didn't complete — run 'tycho install' when you're ready"
    return 0
  fi
  say "next: run 'tycho install' (once, for every repo on this machine)"
}

main "$@"

#!/usr/bin/env bash
# Stamp the Homebrew formula for a released tag and push it to swail-labs/homebrew-tap.
#
# Shared by both callers so the tap has exactly one publish path:
#   - `homebrew-publish` in release.yml — automatic, on every stable tag
#   - `homebrew-tap.yml` — manual, to move the tap for a tag that already shipped
#
# A tap left behind a release is invisible: `brew install` keeps handing out the old version and
# nothing upstream notices. That already happened once — 0.1.1 was cut from a tree that predated
# the publish job, so the tap sat on 0.1.0 until the next release went out.
#
# Usage: publish.sh <tag>          e.g. publish.sh v0.1.2
# Env:   TAP_TOKEN            fine-grained PAT with Contents:write on the tap. The default
#                             GITHUB_TOKEN can't push to another repo, so this can't reuse it.
#        GH_TOKEN             reads THIS repo's release assets
#        GITHUB_REPOSITORY    owner/repo to download the release from
set -euo pipefail

tag="${1:?usage: publish.sh <tag>}"
work="${RUNNER_TEMP:-$(mktemp -d)}"

if [ -z "${TAP_TOKEN:-}" ]; then
  # Fail, don't skip — same reason as npm: `brew upgrade tycho` finding nothing new while the
  # release reads green is a lie the operator never sees.
  echo "HOMEBREW_TAP_TOKEN is not set — cannot stamp the tap for $tag." >&2
  echo "Set the secret, or delete the homebrew-publish job if the tap is not a channel." >&2
  exit 1
fi
# The tap tracks stable only: `brew install` resolves one formula per name, so a prerelease
# landing there would hand every user an RC (npm keeps them off `latest` via the `next`
# dist-tag; a tap has no equivalent).
case "$tag" in *-*) echo "prerelease $tag — the tap tracks stable only, skipping."; exit 0 ;; esac

gh release download "$tag" --repo "${GITHUB_REPOSITORY:?}" --pattern SHA256SUMS --dir "$work" --clobber
# Fails loudly on a missing/renamed asset rather than pushing a half-stamped formula.
# python3, not python: the runner image is not guaranteed to expose an unversioned `python`.
python3 "$(dirname "$0")/stamp.py" "${tag#v}" "$work/SHA256SUMS" -o "$work/tycho.rb"

rm -rf "$work/tap"  # a re-run inside the same RUNNER_TEMP would hit a non-empty clone target
git clone --depth 1 "https://x-access-token:${TAP_TOKEN}@github.com/swail-labs/homebrew-tap.git" "$work/tap"
mkdir -p "$work/tap/Formula"
cp "$work/tycho.rb" "$work/tap/Formula/tycho.rb"
git -C "$work/tap" config user.name "github-actions[bot]"
git -C "$work/tap" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git -C "$work/tap" add Formula/tycho.rb
# Idempotent: re-running the same tag is a no-op instead of a failed empty commit.
if git -C "$work/tap" diff --cached --quiet; then
  echo "tap already at $tag — nothing to push."; exit 0
fi
git -C "$work/tap" commit -m "tycho ${tag#v}"
git -C "$work/tap" push

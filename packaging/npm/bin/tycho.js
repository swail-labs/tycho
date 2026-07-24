#!/usr/bin/env node
// npm/npx wrapper for Tycho (TYCHO-69 §3 / TYCHO-106) — TEMPLATE, not yet publishable.
//
// On first run it downloads the standalone binary matching this package's version + the host
// platform from the GitHub release, VERIFIES it against the release's SHA256SUMS, caches it,
// then execs it forwarding argv and exit code. Zero npm dependencies (Node stdlib only, plus the
// system `tar` present on macOS/Linux/Windows 10+), mirroring Tycho's zero-dependency ethos.
//
// It consumes the SAME assets the `release` job (TYCHO-104) publishes and that the Homebrew
// formula reads: per-target `tycho-<target>.tar.gz` + a `SHA256SUMS` manifest. One artifact set,
// one checksum source of truth.
//
// BLOCKED until (TYCHO-106): the operator owns the `@swail` npm scope + a publish token, and a
// public GitHub release exists whose assets are these tarballs. The package `version` must equal
// the Tycho release it wraps (stamped at publish time from the tag).

"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const https = require("node:https");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const REPO = "swail-labs/tycho";
const { version } = require("../package.json");

// platform/arch -> release tarball basename (must match the `binaries`/`release` job artifacts).
const TARGETS = {
  "darwin-arm64": "tycho-macos-arm64",
  "darwin-x64": "tycho-macos-x86_64",
  "linux-x64": "tycho-linux-x86_64",
  "win32-x64": "tycho-windows-x86_64",
};

function targetName() {
  const key = `${process.platform}-${process.arch}`;
  const name = TARGETS[key];
  if (!name) {
    console.error(`tycho: no prebuilt binary for ${key}. Install via pipx instead: pipx install tycho-cli`);
    process.exit(1);
  }
  return name;
}

// GET a URL into a Buffer, following GitHub's redirect to the asset CDN.
function get(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { "User-Agent": "swail-tycho-npm" } }, (res) => {
      if ([301, 302, 307, 308].includes(res.statusCode)) {
        res.resume(); // drain
        return resolve(get(res.headers.location));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`GET ${url} -> ${res.statusCode}`));
      }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks)));
    }).on("error", reject);
  });
}

function sha256(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

// Pull the expected hash for `filename` out of a `sha256sum`-format manifest. Handles the binary
// marker (`*name`), leading/trailing whitespace, and CRLF. Returns lowercase hex, or null.
function expectedHash(sumsText, filename) {
  for (const line of sumsText.split(/\r?\n/)) {
    const m = line.match(/^([0-9a-fA-F]{64})[ \t]+[* ]?(.+?)\s*$/);
    if (m && m[2] === filename) return m[1].toLowerCase();
  }
  return null;
}

async function ensureBinary() {
  const target = targetName();
  const archive = `${target}.tar.gz`;
  const binName = process.platform === "win32" ? "tycho.exe" : "tycho";
  const cacheDir = path.join(os.homedir(), ".cache", "tycho", version);
  const bin = path.join(cacheDir, binName);
  if (fs.existsSync(bin)) return bin; // already downloaded + verified on a prior run

  fs.mkdirSync(cacheDir, { recursive: true });
  const base = `https://github.com/${REPO}/releases/download/v${version}`;
  const [tarball, sumsText] = await Promise.all([
    get(`${base}/${archive}`),
    get(`${base}/SHA256SUMS`).then((b) => b.toString("utf8")),
  ]);

  // Verify BEFORE writing/extracting — never trust an unverified download on disk.
  const want = expectedHash(sumsText, archive);
  if (!want) throw new Error(`SHA256SUMS has no entry for ${archive}`);
  const got = sha256(tarball);
  if (got !== want) throw new Error(`checksum mismatch for ${archive}: expected ${want}, got ${got}`);

  const tarPath = path.join(cacheDir, archive);
  fs.writeFileSync(tarPath, tarball);
  const r = spawnSync("tar", ["-xzf", tarPath, "-C", cacheDir], { stdio: "inherit" });
  if (r.status !== 0) {
    throw new Error(`tar extraction failed: ${r.error ? r.error.message : `exit ${r.status}`}`);
  }
  fs.unlinkSync(tarPath);
  if (!fs.existsSync(bin)) throw new Error(`archive ${archive} did not contain ${binName}`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function main() {
  ensureBinary()
    .then((bin) => {
      // Announce the channel so `tycho update` knows to upgrade via npm, not the pip fallback
      // (the frozen binary can't infer "I came from npm" from sys.prefix). See cli._upgrade_command.
      const env = { ...process.env, TYCHO_INSTALL: "npm" };
      const r = spawnSync(bin, process.argv.slice(2), { stdio: "inherit", env });
      process.exit(r.status === null ? 1 : r.status);
    })
    .catch((err) => {
      console.error(`tycho: ${err.message}`);
      process.exit(1);
    });
}

if (require.main === module) main();

// Exported for the self-check (test.js). Not part of the public surface.
module.exports = { targetName, sha256, expectedHash };

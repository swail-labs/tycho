"use strict";
// Self-check for the npm wrapper's verification logic. Run: `node test.js`.
// No framework — asserts the checksum parse/compare, the piece that fails silently if wrong.
const assert = require("node:assert");
const { expectedHash, sha256, targetName } = require("./bin/tycho.js");

// A real tarball's bytes hash to a real digest; the manifest must match that exact digest.
const fakeTarball = Buffer.from("pretend this is tycho-linux-x86_64.tar.gz\n");
const digest = sha256(fakeTarball);

// sha256sum-format manifest: binary marker (*), CRLF, a decoy line, extra spacing.
const sums =
  `deadbeef${"0".repeat(56)} *tycho-macos-arm64.tar.gz\r\n` +
  `${digest} *tycho-linux-x86_64.tar.gz\r\n` +
  `feedface${"0".repeat(56)}  tycho-windows-x86_64.tar.gz\n`;

// Finds the right entry and returns lowercase hex.
assert.strictEqual(expectedHash(sums, "tycho-linux-x86_64.tar.gz"), digest);
// Two-space (text-mode) separator also parses.
assert.strictEqual(expectedHash(sums, "tycho-windows-x86_64.tar.gz"), "feedface" + "0".repeat(56));
// A file not in the manifest yields null (→ the wrapper throws, never execs unverified).
assert.strictEqual(expectedHash(sums, "tycho-macos-x86_64.tar.gz"), null);
// A prefix/substring must NOT false-match a different entry.
assert.strictEqual(expectedHash(sums, "tycho-linux-x86_64.tar"), null);

// The end-to-end contract: verify(download) only passes when the bytes match the manifest entry.
assert.strictEqual(sha256(fakeTarball) === expectedHash(sums, "tycho-linux-x86_64.tar.gz"), true);
assert.strictEqual(sha256(Buffer.from("tampered")) === expectedHash(sums, "tycho-linux-x86_64.tar.gz"), false);

// targetName resolves on this host (or exits for an unsupported one — can't assert the exit here).
assert.ok(typeof targetName() === "string" && targetName().startsWith("tycho-"));

console.log("ok - npm wrapper verification self-check passed");

"use strict";

// Spawned by the contract test from desktop cwd with no release arguments.
// Every external operation is replaced; this never builds, installs or starts
// EchoDesk.
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { runCli } = require("./canonical-mac-release.cjs");

function writeApp(appPath, value) {
  fs.mkdirSync(appPath, { recursive: true });
  fs.writeFileSync(path.join(appPath, "fixture-version"), value);
}
function hashes(appPath) {
  const value = fs.readFileSync(path.join(appPath, "fixture-version"));
  const digest = crypto.createHash("sha256").update(value).digest("hex");
  return { executable_sha256: digest, renderer_sha256: digest, backend_sha256: digest };
}

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-zero-arg-contract-"));
  const desktopRoot = path.join(root, "desktop");
  const transactionsRoot = path.join(desktopRoot, ".release-transactions");
  const releaseRoot = path.join(desktopRoot, "release");
  const installedApp = path.join(root, "Applications", "EchoDesk.app");
  const finalRoot = path.join(releaseRoot, "final");
  writeApp(installedApp, "old");
  fs.mkdirSync(finalRoot, { recursive: true });
  fs.writeFileSync(path.join(finalRoot, "old-final"), "old");
  let buildCount = 0;
  let pruneCount = 0;
  const operations = {
    buildCandidate: async (paths) => { buildCount += 1; writeApp(paths.candidateApp, "new"); },
    verifyBundle: hashes,
    copyBundle: (source, target) => fs.cpSync(source, target, { recursive: true }),
    quitInstalledApp: async () => {},
    verifyRuntime: async () => ({ main_count: 1, logical_backend_count: 1, backend_process_count: 2, listener_count: 1, ready_bool: true }),
    prepareFinalRecovery: (paths) => {
      fs.mkdirSync(paths.finalRoot, { recursive: true });
      fs.writeFileSync(path.join(paths.finalRoot, "new-final"), "new");
      return { previous_final_bool: fs.existsSync(paths.previousFinalRoot) };
    },
    verifyFinal: () => {},
    restorePreviousFinal: () => {},
    postCommitPrune: (paths) => {
      pruneCount += 1;
      for (const target of [paths.candidateApp, paths.rollbackApp, paths.previousFinalRoot]) {
        fs.rmSync(target, { recursive: true, force: true });
      }
    },
  };
  const result = await runCli({
    argv: [],
    env: {},
    generation: { transactionsRoot },
    release: { desktopRoot, releaseRoot, installedApp, transactionsRoot },
    operations,
    ownerAlive: () => false,
  });
  console.log(JSON.stringify({
    state: result.state,
    build_count: buildCount,
    prune_count: pruneCount,
    generated_id_valid: /^[a-z0-9][a-z0-9._-]{0,63}$/i.test(result.transaction_id),
  }));
}

main().catch((error) => {
  console.log(JSON.stringify({ error_code: String(error?.code || "fixture_failed") }));
  process.exitCode = 1;
});

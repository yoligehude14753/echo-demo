#!/usr/bin/env node
"use strict";

// The only macOS promotion transaction. Build output and rollback material stay
// intact until commit; cleanup is a post-commit operation only.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { createServer } = require("node:net");
const { randomBytes } = require("node:crypto");
const { spawn, spawnSync } = require("node:child_process");
const { backendFor, resourceHashes, verifyInstalledReleaseManifest, writeInstalledReleaseManifest } = require("./installed-release-manifest.cjs");
const { verifyMacBundle } = require("./mac-bundle-sign.cjs");
const { archiveMatchesInstalled, pruneNoncanonicalRelease } = require("./prune-noncanonical-release.cjs");
const { inspectPackagedRuntime, readProcessTable } = require("./process-tree.cjs");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const RELEASE_ROOT = path.join(DESKTOP_ROOT, "release");
const DEFAULT_APP = "/Applications/EchoDesk.app";
const TRANSACTIONS_ROOT = path.join(DESKTOP_ROOT, ".release-transactions");
const RUNTIME_READY_TIMEOUT_MS = 60_000;
const TERMINAL_STATES = new Set(["pruned", "rolled_back"]);
const PRECOMMIT_STATES = new Set([
  "initiated", "prebuild_preserving", "building", "candidate_verified", "staging_verified",
  "snapshot_preparing", "rollback_ready", "installed", "installed_verified",
  "runtime_verified", "finalizing", "finalized",
]);

function fail(code) {
  const error = new Error(`[canonical-release] ${code}`);
  error.code = code;
  throw error;
}
function safeTransactionId(value) {
  const normalized = String(value || "").trim();
  if (!/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(normalized)) fail("invalid_transaction_id");
  return normalized;
}
function generateCanonicalTransactionId({
  transactionsRoot = TRANSACTIONS_ROOT,
  now = () => new Date(),
  entropy = randomBytes,
  ownerPid = process.pid,
  maxAttempts = 32,
} = {}) {
  const timestamp = now();
  if (!(timestamp instanceof Date) || !Number.isFinite(timestamp.getTime())) fail("invalid_transaction_timestamp");
  if (!Number.isSafeInteger(ownerPid) || ownerPid <= 0) fail("invalid_transaction_owner");
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts <= 0 || maxAttempts > 128) fail("invalid_transaction_attempts");
  const compactUtc = timestamp.toISOString().replace(/[-:.]/g, "").toLowerCase();
  fs.mkdirSync(transactionsRoot, { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const random = Buffer.from(entropy(8));
    if (random.length !== 8) fail("invalid_transaction_entropy");
    const id = safeTransactionId(`r-${compactUtc}-${ownerPid.toString(36)}-${random.toString("hex")}`);
    try {
      fs.mkdirSync(path.join(transactionsRoot, id), { mode: 0o700 });
      return id;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
  }
  fail("transaction_id_collision");
}
function atomicJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 });
  fs.renameSync(temporary, filePath);
}
function readJson(filePath, code) {
  try { return JSON.parse(fs.readFileSync(filePath, "utf8")); } catch { fail(code); }
}
function pidAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error?.code === "EPERM"; }
}
function releasePaths({
  transactionId,
  desktopRoot = DESKTOP_ROOT,
  releaseRoot = path.join(desktopRoot, "release"),
  installedApp = DEFAULT_APP,
  transactionsRoot = path.join(desktopRoot, ".release-transactions"),
} = {}) {
  const id = safeTransactionId(transactionId);
  const transactionRoot = path.join(transactionsRoot, id);
  const applicationsRoot = path.dirname(installedApp);
  return {
    id,
    desktopRoot,
    releaseRoot,
    installedApp,
    candidateApp: path.join(releaseRoot, "mac-arm64", "EchoDesk.app"),
    stagingApp: path.join(applicationsRoot, `.EchoDesk.installing-${id}.app`),
    rollbackApp: path.join(applicationsRoot, `.EchoDesk.rollback-${id}.app`),
    failedApp: path.join(applicationsRoot, `.EchoDesk.failed-${id}.app`),
    transactionsRoot,
    transactionRoot,
    journalPath: path.join(transactionRoot, "journal.json"),
    lockPath: path.join(transactionsRoot, "canonical.lock"),
    finalRoot: path.join(releaseRoot, "final"),
    nextFinalRoot: path.join(transactionRoot, "final-next"),
    previousFinalRoot: path.join(transactionRoot, "final-previous"),
    failedFinalRoot: path.join(transactionRoot, "final-failed"),
  };
}
function journalState(paths) {
  if (!fs.existsSync(paths.journalPath)) return null;
  const value = readJson(paths.journalPath, "journal_invalid");
  if (value?.schema !== "echodesk-canonical-release-v1" || value?.transaction_id !== paths.id) fail("journal_invalid");
  return value;
}
function writeJournal(paths, journal, state, extra = {}) {
  const next = { ...journal, ...extra, state, updated_at_ms: Date.now() };
  atomicJson(paths.journalPath, next);
  return next;
}
function acquireLock(paths, { ownerAlive = pidAlive, ownerPid = process.pid } = {}) {
  fs.mkdirSync(paths.transactionsRoot, { recursive: true });
  const fresh = { schema: "echodesk-canonical-release-lock-v1", transaction_id: paths.id, owner_pid: ownerPid };
  try {
    const descriptor = fs.openSync(paths.lockPath, "wx", 0o600);
    fs.writeFileSync(descriptor, `${JSON.stringify(fresh)}\n`);
    fs.closeSync(descriptor);
    return { takeover: false };
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }
  const current = readJson(paths.lockPath, "lock_invalid");
  if (ownerAlive(Number(current?.owner_pid))) fail("lock_busy");
  const currentId = safeTransactionId(current?.transaction_id);
  const currentJournalPath = path.join(paths.transactionsRoot, currentId, "journal.json");
  const currentJournal = fs.existsSync(currentJournalPath) ? readJson(currentJournalPath, "journal_invalid") : null;
  if (currentId === paths.id && currentJournal && !TERMINAL_STATES.has(currentJournal.state)) {
    // Crash recovery takes over the existing transaction lock in place. It is
    // not recycled for another transaction.
    atomicJson(paths.lockPath, { ...fresh, recovered_owner_pid: Number(current.owner_pid) || 0 });
    return { takeover: true };
  }
  if (!currentJournal || !TERMINAL_STATES.has(currentJournal.state)) fail("lock_recovery_required");
  fs.rmSync(paths.lockPath, { force: true });
  const descriptor = fs.openSync(paths.lockPath, "wx", 0o600);
  fs.writeFileSync(descriptor, `${JSON.stringify(fresh)}\n`);
  fs.closeSync(descriptor);
  return { takeover: false };
}
function releaseLock(paths, ownerPid = process.pid) {
  if (!fs.existsSync(paths.lockPath)) return;
  const current = readJson(paths.lockPath, "lock_invalid");
  const journal = journalState(paths);
  if (current?.transaction_id !== paths.id || Number(current?.owner_pid) !== ownerPid) fail("lock_owner_mismatch");
  if (!journal || !TERMINAL_STATES.has(journal.state)) fail("lock_release_before_terminal");
  fs.rmSync(paths.lockPath, { force: true });
}
function sameHashes(left, right) {
  return ["executable_sha256", "renderer_sha256", "backend_sha256"]
    .every((field) => typeof left?.[field] === "string" && left[field] === right?.[field]);
}
function verifyBundle(appPath) {
  if (!fs.existsSync(appPath)) fail("candidate_missing");
  verifyMacBundle(appPath);
  return resourceHashes(appPath);
}
function runChecked(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: options.cwd, env: options.env || process.env, stdio: options.stdio || "inherit", encoding: options.encoding });
  if (result.error || result.status !== 0) fail(options.code || "command_failed");
  return result;
}
function normalizeRuntimeEndpoint(raw) {
  const value = String(raw || "").trim();
  if (!value) return null;
  let parsed;
  try { parsed = new URL(value); } catch { return null; }
  if (
    parsed.protocol !== "http:" ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    !["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)
  ) return null;
  const port = Number(parsed.port);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) return null;
  return parsed.origin;
}
function runtimeRecordPath(env = process.env) {
  const root = String(env.ECHO_USER_DIR || path.join(os.homedir(), ".echodesk"));
  return path.join(path.resolve(root), "runtime", "endpoint.json");
}
function readLiveRuntimeEndpoint(env = process.env, readFile = fs.readFileSync) {
  let record;
  try { record = JSON.parse(readFile(runtimeRecordPath(env), "utf8")); } catch { return null; }
  if (!Number.isSafeInteger(Number(record?.pid)) || !pidAlive(Number(record.pid))) return null;
  return normalizeRuntimeEndpoint(record?.base_url);
}
async function allocateLoopbackEndpoint(serverFactory = createServer) {
  return new Promise((resolve, reject) => {
    const server = serverFactory();
    const finish = (error, endpoint) => {
      server.close(() => {
        if (error) reject(error);
        else resolve(endpoint);
      });
    };
    server.once("error", (error) => finish(error));
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      if (!Number.isSafeInteger(port) || port < 1) {
        finish(new Error("runtime_endpoint_allocate_failed"));
        return;
      }
      finish(null, `http://127.0.0.1:${port}`);
    });
  });
}
async function resolveCanonicalRuntimeEnv({
  env = process.env,
  readFile = fs.readFileSync,
  allocate = allocateLoopbackEndpoint,
} = {}) {
  const runtimeEnv = { ...env };
  const explicit = normalizeRuntimeEndpoint(runtimeEnv.ECHODESK_BASE_URL);
  if (explicit) return { env: { ...runtimeEnv, ECHODESK_BASE_URL: explicit }, source: "environment" };
  const record = readLiveRuntimeEndpoint(runtimeEnv, readFile);
  if (record) return { env: { ...runtimeEnv, ECHODESK_BASE_URL: record }, source: "runtime_record" };
  const allocated = normalizeRuntimeEndpoint(await allocate());
  if (!allocated) throw Object.assign(new Error("runtime_endpoint_allocate_failed"), { code: "runtime_endpoint_allocate_failed" });
  return { env: { ...runtimeEnv, ECHODESK_BASE_URL: allocated }, source: "transaction_ephemeral" };
}
function buildCandidate(paths, runtimeEnv = process.env) {
  runChecked("npm", ["run", "app:dist:mac"], { cwd: paths.desktopRoot, env: runtimeEnv, code: "candidate_build_failed" });
}
function copyBundle(source, target) {
  if (fs.existsSync(target)) fs.rmSync(target, { recursive: true, force: true });
  runChecked("/usr/bin/ditto", [source, target], { code: "bundle_copy_failed" });
}
function processIdsByName(name) {
  return readProcessTable().filter((row) => path.basename(row.comm) === name).map((row) => row.pid);
}
async function delay(milliseconds) { await new Promise((resolve) => setTimeout(resolve, milliseconds)); }
async function quitInstalledApp() {
  spawnSync("/usr/bin/osascript", ["-e", 'tell application id "com.echodesk.app" to quit'], { stdio: "ignore" });
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (processIdsByName("EchoDesk").length === 0) return;
    await delay(200);
  }
  fail("installed_app_did_not_quit");
}
async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  const deadline = Date.now() + 5_000;
  while (child.exitCode === null && Date.now() < deadline) await delay(100);
  if (child.exitCode === null) fail("runtime_did_not_stop");
}
async function verifyRuntime(appPath, {
  env = process.env,
  launch = (executable) => spawn(executable, [], { env, stdio: "ignore" }),
  inspect = inspectPackagedRuntime,
  now = Date.now,
  pollDelay = delay,
  timeoutMs = RUNTIME_READY_TIMEOUT_MS,
} = {}) {
  const executable = path.join(appPath, "Contents", "MacOS", "EchoDesk");
  const backend = backendFor(appPath);
  const child = launch(executable);
  if (!child || !Number.isSafeInteger(child.pid) || child.pid <= 0) fail("runtime_launch_failed");
  let latest = { main_count: 0, logical_backend_count: 0, backend_process_count: 0, listener_count: 0, process_tree_pid_count: 0, ready_bool: false };
  try {
    const deadline = now() + timeoutMs;
    while (now() < deadline) {
      if (child.exitCode !== null || child.signalCode) fail("runtime_exited");
      latest = inspect(child.pid, backend);
      if (latest.ready_bool) return latest;
      await pollDelay(200);
    }
    fail("runtime_not_ready");
  } finally {
    await stopChild(child);
  }
}
function verifyRecoveryDirectory(directory, appPath) {
  const archive = path.join(directory, "EchoDesk-current-recovery.zip");
  const manifest = path.join(directory, "installed-release-manifest.json");
  const entries = fs.existsSync(directory)
    ? fs.readdirSync(directory, { withFileTypes: true })
    : [];
  const allowed = new Set([path.basename(archive), path.basename(manifest)]);
  if (entries.length !== allowed.size || entries.some((entry) => !entry.isFile() || !allowed.has(entry.name))) fail("recovery_layout_ambiguous");
  if (!fs.existsSync(archive) || !fs.existsSync(manifest)) fail("recovery_layout_missing");
  if (!archiveMatchesInstalled(archive, appPath)) fail("recovery_hash_mismatch");
  verifyInstalledReleaseManifest({ appPath, manifestPath: manifest });
  return { final_archive_count: 1, resource_count: 3 };
}
function prepareFinalRecovery(paths) {
  if (fs.existsSync(paths.nextFinalRoot)) fs.rmSync(paths.nextFinalRoot, { recursive: true, force: true });
  fs.mkdirSync(paths.nextFinalRoot, { recursive: true });
  const archive = path.join(paths.nextFinalRoot, "EchoDesk-current-recovery.zip");
  runChecked("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", paths.installedApp, archive], { code: "recovery_archive_failed" });
  writeInstalledReleaseManifest({ appPath: paths.installedApp, manifestPath: path.join(paths.nextFinalRoot, "installed-release-manifest.json") });
  verifyRecoveryDirectory(paths.nextFinalRoot, paths.installedApp);
  if (fs.existsSync(paths.finalRoot)) {
    try {
      verifyRecoveryDirectory(paths.finalRoot, paths.installedApp);
      fs.rmSync(paths.nextFinalRoot, { recursive: true, force: true });
      return { previous_final_bool: fs.existsSync(paths.previousFinalRoot) };
    } catch {
      if (fs.existsSync(paths.previousFinalRoot)) fail("previous_final_ambiguous");
      fs.renameSync(paths.finalRoot, paths.previousFinalRoot);
    }
  }
  fs.renameSync(paths.nextFinalRoot, paths.finalRoot);
  verifyRecoveryDirectory(paths.finalRoot, paths.installedApp);
  return { previous_final_bool: fs.existsSync(paths.previousFinalRoot) };
}
function restorePreviousFinal(paths, journal = {}) {
  if (fs.existsSync(paths.previousFinalRoot)) {
    if (fs.existsSync(paths.failedFinalRoot)) fs.rmSync(paths.failedFinalRoot, { recursive: true, force: true });
    if (fs.existsSync(paths.finalRoot)) fs.renameSync(paths.finalRoot, paths.failedFinalRoot);
    fs.renameSync(paths.previousFinalRoot, paths.finalRoot);
    if (fs.existsSync(paths.failedFinalRoot)) fs.rmSync(paths.failedFinalRoot, { recursive: true, force: true });
  } else if (journal.original_final_bool === false && fs.existsSync(paths.finalRoot)) {
    // The transaction created the first final layout; it is not valid after an
    // install rollback.
    fs.rmSync(paths.finalRoot, { recursive: true, force: true });
  }
}
async function rollbackPrecommit(paths, journal, operations) {
  await operations.quitInstalledApp();
  if (fs.existsSync(paths.rollbackApp)) {
    if (fs.existsSync(paths.failedApp)) fs.rmSync(paths.failedApp, { recursive: true, force: true });
    if (fs.existsSync(paths.installedApp)) fs.renameSync(paths.installedApp, paths.failedApp);
    fs.renameSync(paths.rollbackApp, paths.installedApp);
    // Snapshot is no longer deleted: the exact same directory has been restored
    // as the canonical installed application before failed bytes are removed.
    if (fs.existsSync(paths.failedApp)) fs.rmSync(paths.failedApp, { recursive: true, force: true });
  } else if (journal.original_installed_bool === false && fs.existsSync(paths.installedApp)) {
    fs.rmSync(paths.installedApp, { recursive: true, force: true });
  }
  operations.restorePreviousFinal(paths, journal);
  if (fs.existsSync(paths.stagingApp)) fs.rmSync(paths.stagingApp, { recursive: true, force: true });
}
function postCommitPrune(paths) {
  const result = pruneNoncanonicalRelease({
    releaseRoot: paths.releaseRoot,
    appPath: paths.installedApp,
    workspaceRoot: paths.desktopRoot,
    candidatePromoted: true,
  });
  for (const target of [paths.rollbackApp, paths.stagingApp, paths.failedApp, paths.previousFinalRoot, paths.nextFinalRoot, paths.failedFinalRoot]) {
    if (fs.existsSync(target)) fs.rmSync(target, { recursive: true, force: true });
  }
  return result;
}
function defaultOperations() {
  return { buildCandidate, copyBundle, postCommitPrune, prepareFinalRecovery, quitInstalledApp, restorePreviousFinal, verifyBundle, verifyFinal: verifyRecoveryDirectory, verifyRuntime };
}

async function runCanonicalMacRelease({ transactionId, paths: suppliedPaths, operations: suppliedOperations, ownerAlive, ownerPid = process.pid, runtimeEnv = process.env } = {}) {
  const paths = suppliedPaths || releasePaths({ transactionId });
  const operations = { ...defaultOperations(), ...(suppliedOperations || {}) };
  acquireLock(paths, { ownerAlive, ownerPid });
  let journal = journalState(paths) || {
    schema: "echodesk-canonical-release-v1",
    transaction_id: paths.id,
    state: "initiated",
    created_at_ms: Date.now(),
    updated_at_ms: Date.now(),
    build_count: 0,
    prune_count: 0,
  };
  if (!fs.existsSync(paths.journalPath)) atomicJson(paths.journalPath, journal);
  if (TERMINAL_STATES.has(journal.state)) {
    releaseLock(paths, ownerPid);
    return journal;
  }
  try {
    if (journal.state === "initiated") {
      journal = writeJournal(paths, journal, "prebuild_preserving", { original_final_bool: fs.existsSync(paths.finalRoot) });
    }
    if (journal.state === "prebuild_preserving") {
      if (journal.original_final_bool && !fs.existsSync(paths.previousFinalRoot)) {
        if (!fs.existsSync(paths.finalRoot)) fail("final_missing_before_build");
        fs.renameSync(paths.finalRoot, paths.previousFinalRoot);
      }
      journal = writeJournal(paths, journal, "building", { build_count: journal.build_count + 1 });
      await operations.buildCandidate(paths, runtimeEnv);
    }
    if (journal.state === "building") {
      if (!fs.existsSync(paths.candidateApp)) fail("candidate_missing");
      const candidateHashes = operations.verifyBundle(paths.candidateApp);
      journal = writeJournal(paths, journal, "candidate_verified", { candidate_hashes: candidateHashes });
    }
    if (PRECOMMIT_STATES.has(journal.state)) {
      if (!fs.existsSync(paths.candidateApp)) fail("candidate_missing");
      const currentCandidateHashes = operations.verifyBundle(paths.candidateApp);
      if (!sameHashes(currentCandidateHashes, journal.candidate_hashes)) fail("candidate_changed");
    }
    if (journal.state === "candidate_verified") {
      await operations.quitInstalledApp();
      operations.copyBundle(paths.candidateApp, paths.stagingApp);
      const stagingHashes = operations.verifyBundle(paths.stagingApp);
      if (!sameHashes(stagingHashes, journal.candidate_hashes)) fail("staging_hash_mismatch");
      journal = writeJournal(paths, journal, "staging_verified", { original_installed_bool: fs.existsSync(paths.installedApp) });
    }
    if (journal.state === "staging_verified") journal = writeJournal(paths, journal, "snapshot_preparing");
    if (journal.state === "snapshot_preparing") {
      if (journal.original_installed_bool && !fs.existsSync(paths.rollbackApp)) {
        if (!fs.existsSync(paths.installedApp)) fail("installed_missing_before_snapshot");
        fs.renameSync(paths.installedApp, paths.rollbackApp);
      }
      journal = writeJournal(paths, journal, "rollback_ready");
    }
    if (journal.state === "rollback_ready") {
      if (fs.existsSync(paths.installedApp)) {
        const installedHashes = operations.verifyBundle(paths.installedApp);
        if (!sameHashes(installedHashes, journal.candidate_hashes)) fail("interrupted_install_ambiguous");
      } else {
        if (!fs.existsSync(paths.stagingApp)) fail("staging_missing");
        fs.renameSync(paths.stagingApp, paths.installedApp);
      }
      journal = writeJournal(paths, journal, "installed");
    }
    if (journal.state === "installed") {
      const installedHashes = operations.verifyBundle(paths.installedApp);
      if (!sameHashes(installedHashes, journal.candidate_hashes)) fail("installed_hash_mismatch");
      journal = writeJournal(paths, journal, "installed_verified");
    }
    if (journal.state === "installed_verified") {
      const runtime = await operations.verifyRuntime(paths.installedApp, { env: runtimeEnv });
      if (runtime?.main_count !== 1 || runtime?.logical_backend_count !== 1 || runtime?.listener_count !== 1 || runtime?.ready_bool !== true) fail("runtime_topology_invalid");
      journal = writeJournal(paths, journal, "runtime_verified", {
        runtime: {
          main_count: runtime.main_count,
          logical_backend_count: runtime.logical_backend_count,
          backend_process_count: runtime.backend_process_count,
          listener_count: runtime.listener_count,
        },
      });
    }
    if (journal.state === "runtime_verified") journal = writeJournal(paths, journal, "finalizing", {
      original_final_bool: typeof journal.original_final_bool === "boolean" ? journal.original_final_bool : fs.existsSync(paths.finalRoot),
    });
    if (journal.state === "finalizing") {
      const final = operations.prepareFinalRecovery(paths);
      operations.verifyFinal(paths.finalRoot, paths.installedApp);
      journal = writeJournal(paths, journal, "finalized", final);
    }
    if (journal.state === "finalized") {
      const installedHashes = operations.verifyBundle(paths.installedApp);
      if (!sameHashes(installedHashes, journal.candidate_hashes)) fail("installed_hash_mismatch");
      operations.verifyFinal(paths.finalRoot, paths.installedApp);
      journal = writeJournal(paths, journal, "committed");
    }
    if (journal.state === "committed") {
      operations.postCommitPrune(paths);
      journal = writeJournal(paths, journal, "pruned", { prune_count: journal.prune_count + 1 });
    }
    releaseLock(paths, ownerPid);
    return journal;
  } catch (error) {
    if (journal.state !== "committed" && journal.state !== "pruned") {
      try {
        await rollbackPrecommit(paths, journal, operations);
        journal = writeJournal(paths, journal, "rolled_back", { error_code: String(error?.code || "release_failed").slice(0, 64) });
        releaseLock(paths, ownerPid);
      } catch (rollbackError) {
        writeJournal(paths, journal, "rollback_failed", { error_code: String(error?.code || "release_failed").slice(0, 64), rollback_error_code: String(rollbackError?.code || "rollback_failed").slice(0, 64) });
      }
    }
    throw error;
  }
}

function cliTransactionId(argv = process.argv.slice(2), env = process.env, generation = {}) {
  if (argv.length > 0) {
    if (argv.length !== 2 || argv[0] !== "--transaction") fail("invalid_transaction_arguments");
    return safeTransactionId(argv[1]);
  }
  const configured = String(env.ECHODESK_RELEASE_TRANSACTION_ID || "").trim();
  if (configured) return safeTransactionId(configured);
  return generateCanonicalTransactionId(generation);
}
async function runCli({
  argv = process.argv.slice(2),
  env = process.env,
  generation,
  release = {},
  operations,
  ownerAlive,
  ownerPid = process.pid,
} = {}) {
  const transactionId = cliTransactionId(argv, env, { ownerPid, ...(generation || {}) });
  const paths = releasePaths({ transactionId, ...release });
  const resolved = await resolveCanonicalRuntimeEnv({ env });
  return runCanonicalMacRelease({ paths, operations, ownerAlive, ownerPid, runtimeEnv: resolved.env });
}
if (require.main === module) {
  runCli()
    .then((result) => console.log(JSON.stringify({ state: result.state, build_count: result.build_count, prune_count: result.prune_count, runtime: result.runtime || null })))
    .catch((error) => { console.error(`[canonical-release] ${String(error?.code || "release_failed")}`); process.exitCode = 1; });
}

module.exports = {
  PRECOMMIT_STATES,
  RUNTIME_READY_TIMEOUT_MS,
  TERMINAL_STATES,
  acquireLock,
  cliTransactionId,
  defaultOperations,
  generateCanonicalTransactionId,
  pidAlive,
  releaseLock,
  releasePaths,
  rollbackPrecommit,
  runCli,
  runCanonicalMacRelease,
  resolveCanonicalRuntimeEnv,
  sameHashes,
  verifyRuntime,
};

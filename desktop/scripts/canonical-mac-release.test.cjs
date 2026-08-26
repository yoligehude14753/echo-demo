"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");
const {
  acquireLock,
  cliTransactionId,
  generateCanonicalTransactionId,
  RUNTIME_READY_TIMEOUT_MS,
  releaseLock,
  releasePaths,
  resolveCanonicalRuntimeEnv,
  runCanonicalMacRelease,
  verifyRuntime,
} = require("./canonical-mac-release.cjs");
const { inspectPackagedRuntime } = require("./process-tree.cjs");

function writeApp(appPath, value) {
  fs.mkdirSync(appPath, { recursive: true });
  fs.writeFileSync(path.join(appPath, "fixture-version"), value);
}
function readVersion(appPath) {
  return fs.readFileSync(path.join(appPath, "fixture-version"), "utf8");
}
function hashes(appPath) {
  const digest = crypto.createHash("sha256").update(readVersion(appPath)).digest("hex");
  return { executable_sha256: digest, renderer_sha256: digest, backend_sha256: digest };
}
function fixture(id = "f186-test") {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-canonical-release-"));
  const desktopRoot = path.join(root, "desktop");
  const installedApp = path.join(root, "Applications", "EchoDesk.app");
  const paths = releasePaths({ transactionId: id, desktopRoot, installedApp });
  fs.mkdirSync(paths.releaseRoot, { recursive: true });
  writeApp(installedApp, "old");
  fs.mkdirSync(paths.finalRoot, { recursive: true });
  fs.writeFileSync(path.join(paths.finalRoot, "old-final"), "old");
  return { root, paths };
}
function operations(paths, overrides = {}) {
  const counters = { build: 0, prune: 0, verifyFinal: 0 };
  const value = {
    counters,
    buildCandidate: async () => { counters.build += 1; writeApp(paths.candidateApp, "new"); },
    verifyBundle: (appPath) => {
      if (!fs.existsSync(appPath)) { const error = new Error("candidate_missing"); error.code = "candidate_missing"; throw error; }
      return hashes(appPath);
    },
    copyBundle: (source, target) => fs.cpSync(source, target, { recursive: true }),
    quitInstalledApp: async () => {},
    verifyRuntime: async () => ({ main_count: 1, logical_backend_count: 1, backend_process_count: 2, listener_count: 1, ready_bool: true }),
    prepareFinalRecovery: () => {
      fs.mkdirSync(paths.finalRoot, { recursive: true });
      fs.writeFileSync(path.join(paths.finalRoot, "new-final"), "new");
      return { previous_final_bool: fs.existsSync(paths.previousFinalRoot) };
    },
    verifyFinal: () => { counters.verifyFinal += 1; },
    restorePreviousFinal: () => {
      if (!fs.existsSync(paths.previousFinalRoot)) return;
      fs.rmSync(paths.finalRoot, { recursive: true, force: true });
      fs.renameSync(paths.previousFinalRoot, paths.finalRoot);
    },
    postCommitPrune: () => {
      counters.prune += 1;
      fs.rmSync(paths.candidateApp, { recursive: true, force: true });
      fs.rmSync(paths.rollbackApp, { recursive: true, force: true });
      fs.rmSync(paths.previousFinalRoot, { recursive: true, force: true });
    },
    ...overrides,
  };
  return value;
}

test("zero-argument child process enters the transaction with fake adapters", () => {
  const desktopRoot = path.resolve(__dirname, "..");
  const result = spawnSync(process.execPath, ["scripts/canonical-mac-release.zero-arg-fixture.cjs"], {
    cwd: desktopRoot,
    encoding: "utf8",
    env: { PATH: process.env.PATH || "" },
  });
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout.trim());
  assert.deepEqual(report, {
    state: "pruned",
    build_count: 1,
    prune_count: 1,
    generated_id_valid: true,
  });
});

test("canonical zero-argument runtime env prefers a live owner record", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-runtime-record-"));
  const endpointPath = path.join(root, "runtime", "endpoint.json");
  fs.mkdirSync(path.dirname(endpointPath), { recursive: true });
  fs.writeFileSync(endpointPath, JSON.stringify({
    schema_version: 1,
    base_url: "http://127.0.0.1:19345",
    pid: process.pid,
  }));
  const resolved = await resolveCanonicalRuntimeEnv({
    env: { ECHO_USER_DIR: root },
    allocate: async () => { throw new Error("allocator must not run"); },
  });
  assert.equal(resolved.source, "runtime_record");
  assert.equal(resolved.env.ECHODESK_BASE_URL, "http://127.0.0.1:19345");
});

test("canonical zero-argument runtime env allocates a loopback endpoint when no owner is live", async () => {
  const resolved = await resolveCanonicalRuntimeEnv({
    env: { ECHO_USER_DIR: fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-no-runtime-")) },
    allocate: async () => "http://127.0.0.1:19346",
  });
  assert.equal(resolved.source, "transaction_ephemeral");
  assert.equal(resolved.env.ECHODESK_BASE_URL, "http://127.0.0.1:19346");
});

test("canonical generator strips timezone punctuation and retains millisecond identity", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-id-format-"));
  const id = generateCanonicalTransactionId({
    transactionsRoot: root,
    now: () => new Date("2026-07-29T23:59:59.007+08:00"),
    entropy: () => Buffer.from("0011223344556677", "hex"),
    ownerPid: 12345,
  });
  assert.equal(id, "r-20260729t155959007z-9ix-0011223344556677");
  assert.match(id, /^[a-z0-9][a-z0-9._-]{0,63}$/);
  assert.doesNotMatch(id, /[:+]/);
  assert.equal(fs.statSync(path.join(root, id)).isDirectory(), true);
});

test("canonical generator atomically avoids collisions and stays unique under concurrent requests", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-id-collision-"));
  const now = () => new Date("2026-07-29T00:00:00.000Z");
  const first = generateCanonicalTransactionId({ transactionsRoot: root, now, entropy: () => Buffer.alloc(8), ownerPid: 77 });
  let call = 0;
  const second = generateCanonicalTransactionId({
    transactionsRoot: root,
    now,
    entropy: () => Buffer.alloc(8, call++ === 0 ? 0 : 1),
    ownerPid: 77,
  });
  assert.notEqual(second, first);
  const ids = await Promise.all(Array.from({ length: 64 }, async () => generateCanonicalTransactionId({ transactionsRoot: root, now, ownerPid: 77 })));
  assert.equal(new Set(ids).size, 64);
  assert.equal(ids.every((id) => /^[a-z0-9][a-z0-9._-]{0,63}$/.test(id)), true);
});

test("CLI preserves explicit legal IDs and rejects every malformed explicit value", () => {
  assert.equal(cliTransactionId(["--transaction", "f186.retry-1"], {}), "f186.retry-1");
  assert.equal(cliTransactionId([], { ECHODESK_RELEASE_TRANSACTION_ID: "f186_env" }), "f186_env");
  for (const argv of [
    ["--transaction"],
    ["--transaction", "bad/id"],
    ["--transaction", "bad:id"],
    ["--transaction", "时区"],
    ["--transaction", `x${"a".repeat(64)}`],
    ["unexpected"],
    ["--transaction", "valid", "extra"],
  ]) assert.throws(() => cliTransactionId(argv, {}), /invalid_transaction/);
  assert.throws(() => cliTransactionId([], { ECHODESK_RELEASE_TRANSACTION_ID: "bad/value" }), /invalid_transaction_id/);
});

test("runtime failure preserves candidate, restores rollback, and never prunes", async () => {
  const { paths } = fixture("runtime-fail");
  const ops = operations(paths, {
    verifyRuntime: async () => { const error = new Error("runtime_not_ready"); error.code = "runtime_not_ready"; throw error; },
  });
  await assert.rejects(
    runCanonicalMacRelease({ paths, operations: ops, ownerAlive: () => false, ownerPid: 8001 }),
    (error) => error.code === "runtime_not_ready",
  );
  assert.equal(readVersion(paths.installedApp), "old");
  assert.equal(readVersion(paths.candidateApp), "new");
  assert.equal(fs.existsSync(paths.rollbackApp), false);
  assert.equal(ops.counters.prune, 0);
  assert.equal(fs.existsSync(path.join(paths.finalRoot, "old-final")), true);
  const journal = JSON.parse(fs.readFileSync(paths.journalPath, "utf8"));
  assert.equal(journal.state, "rolled_back");
  assert.equal(journal.build_count, 1);
  assert.equal(journal.prune_count, 0);
});

test("runtime verification allows a valid cold start after the former 30 second boundary", async () => {
  let clock = 0;
  const child = {
    pid: 9001,
    exitCode: null,
    signalCode: null,
    kill() { this.exitCode = 0; },
  };
  const runtime = await verifyRuntime("/Applications/EchoDesk.app", {
    launch: () => child,
    inspect: () => ({
      main_count: 1,
      logical_backend_count: 1,
      backend_process_count: 2,
      listener_count: clock >= 40_000 ? 1 : 0,
      process_tree_pid_count: 3,
      ready_bool: clock >= 40_000,
    }),
    now: () => clock,
    pollDelay: async (milliseconds) => { clock += milliseconds; },
  });
  assert.equal(RUNTIME_READY_TIMEOUT_MS, 60_000);
  assert.equal(runtime.ready_bool, true);
  assert.equal(clock, 40_000);
  assert.equal(child.exitCode, 0);
});

test("successful transaction builds once, commits before exactly one prune, and is repeat-idempotent", async () => {
  const { paths } = fixture("success");
  const events = [];
  const ops = operations(paths, {
    postCommitPrune: () => {
      const journal = JSON.parse(fs.readFileSync(paths.journalPath, "utf8"));
      events.push(`prune:${journal.state}`);
      ops.counters.prune += 1;
      fs.rmSync(paths.candidateApp, { recursive: true, force: true });
      fs.rmSync(paths.rollbackApp, { recursive: true, force: true });
      fs.rmSync(paths.previousFinalRoot, { recursive: true, force: true });
    },
  });
  const first = await runCanonicalMacRelease({ paths, operations: ops, ownerAlive: () => false, ownerPid: 8002 });
  const second = await runCanonicalMacRelease({ paths, operations: ops, ownerAlive: () => false, ownerPid: 8002 });
  assert.equal(first.state, "pruned");
  assert.equal(second.state, "pruned");
  assert.equal(first.build_count, 1);
  assert.equal(first.prune_count, 1);
  assert.equal(ops.counters.build, 1);
  assert.equal(ops.counters.prune, 1);
  assert.deepEqual(events, ["prune:committed"]);
  assert.equal(readVersion(paths.installedApp), "new");
});

test("crash journal resumes without a second build and a parent-child backend is one logical backend", async () => {
  const { paths } = fixture("resume");
  writeApp(paths.candidateApp, "new");
  fs.mkdirSync(paths.transactionRoot, { recursive: true });
  fs.renameSync(paths.finalRoot, paths.previousFinalRoot);
  fs.writeFileSync(paths.journalPath, JSON.stringify({
    schema: "echodesk-canonical-release-v1",
    transaction_id: paths.id,
    state: "building",
    original_final_bool: true,
    build_count: 1,
    prune_count: 0,
    created_at_ms: 1,
    updated_at_ms: 1,
  }));
  fs.writeFileSync(paths.lockPath, JSON.stringify({
    schema: "echodesk-canonical-release-lock-v1",
    transaction_id: paths.id,
    owner_pid: 7001,
  }));
  const ops = operations(paths);
  const result = await runCanonicalMacRelease({ paths, operations: ops, ownerAlive: () => false, ownerPid: 8003 });
  assert.equal(result.state, "pruned");
  assert.equal(result.build_count, 1);
  assert.equal(ops.counters.build, 0);

  const rows = [
    { pid: 100, ppid: 1, comm: "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk" },
    { pid: 110, ppid: 100, comm: "/Applications/EchoDesk.app/Contents/Resources/backend/echodesk-backend" },
    { pid: 111, ppid: 110, comm: "/Applications/EchoDesk.app/Contents/Resources/backend/echodesk-backend" },
  ];
  assert.deepEqual(
    inspectPackagedRuntime(100, "/Applications/EchoDesk.app/Contents/Resources/backend/echodesk-backend", {
      listProcesses: () => rows,
      listListeningPids: () => [111],
    }),
    {
      main_count: 1,
      logical_backend_count: 1,
      backend_process_count: 2,
      listener_count: 1,
      process_tree_pid_count: 3,
      ready_bool: true,
    },
  );
});

test("missing candidate fails closed and stale lock cannot be recycled from a nonterminal foreign transaction", async () => {
  const { paths } = fixture("missing-candidate");
  const ops = operations(paths, { buildCandidate: async () => { ops.counters.build += 1; } });
  await assert.rejects(
    runCanonicalMacRelease({ paths, operations: ops, ownerAlive: () => false, ownerPid: 8004 }),
    (error) => error.code === "candidate_missing",
  );
  assert.equal(readVersion(paths.installedApp), "old");
  assert.equal(ops.counters.prune, 0);

  const other = releasePaths({ transactionId: "other", desktopRoot: paths.desktopRoot, installedApp: paths.installedApp });
  fs.mkdirSync(other.transactionRoot, { recursive: true });
  fs.writeFileSync(other.journalPath, JSON.stringify({ schema: "echodesk-canonical-release-v1", transaction_id: other.id, state: "installed" }));
  fs.writeFileSync(paths.lockPath, JSON.stringify({ schema: "echodesk-canonical-release-lock-v1", transaction_id: other.id, owner_pid: 7002 }));
  const next = releasePaths({ transactionId: "next", desktopRoot: paths.desktopRoot, installedApp: paths.installedApp });
  assert.throws(
    () => acquireLock(next, { ownerAlive: () => false, ownerPid: 8005 }),
    (error) => error.code === "lock_recovery_required",
  );

  fs.writeFileSync(other.journalPath, JSON.stringify({ schema: "echodesk-canonical-release-v1", transaction_id: other.id, state: "rolled_back" }));
  const acquired = acquireLock(next, { ownerAlive: () => false, ownerPid: 8005 });
  assert.deepEqual(acquired, { takeover: false });
  fs.mkdirSync(next.transactionRoot, { recursive: true });
  fs.writeFileSync(next.journalPath, JSON.stringify({ schema: "echodesk-canonical-release-v1", transaction_id: next.id, state: "rolled_back" }));
  releaseLock(next, 8005);
});

test("release and runtime sources contain no pgrep, grep, plutil, or PCRE noncapturing checker", () => {
  for (const filename of ["canonical-mac-release.cjs", "process-tree.cjs", "core-e2e-acceptance.cjs"]) {
    const source = fs.readFileSync(path.join(__dirname, filename), "utf8");
    assert.doesNotMatch(source, /\b(?:pgrep|grep|plutil)\b/);
    assert.doesNotMatch(source, /pgrep[\s\S]{0,160}\(\?:/);
  }
  const releaseSource = fs.readFileSync(path.join(__dirname, "canonical-mac-release.cjs"), "utf8");
  assert.doesNotMatch(releaseSource, /const value = index >= 0[\s\S]{0,160}safeTransactionId\(value\)/);
  assert.match(releaseSource, /return generateCanonicalTransactionId\(generation\)/);
});

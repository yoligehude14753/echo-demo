const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createBackendLifecycleController,
} = require("../backend-lifecycle-controller.cjs");

function spawn(controller, child) {
  const attempt = controller.beginSpawn();
  return controller.attachChild(attempt, child);
}

test("PyInstaller supervisor parent owns one logical backend lifecycle", async () => {
  const controller = createBackendLifecycleController();
  const supervisor = { pid: 101, runtimeChildPid: 102 };
  const lease = spawn(controller, supervisor);
  assert.equal(controller.currentChild(), supervisor);
  assert.equal(controller.settleHealth(lease, true).state, "ready");
  assert.equal(controller.isReady(lease), true);
});

test("late health completion from an old child cannot commit current readiness", () => {
  const controller = createBackendLifecycleController();
  const oldLease = spawn(controller, { pid: 201 });
  controller.invalidate(oldLease);
  const currentLease = spawn(controller, { pid: 202 });
  assert.equal(controller.settleHealth(oldLease, true).state, "stale");
  assert.equal(controller.isReady(currentLease), false);
});

test("late exit from an old child cannot retire its replacement", () => {
  const controller = createBackendLifecycleController();
  const oldLease = spawn(controller, { pid: 301 });
  controller.invalidate(oldLease);
  const replacement = { pid: 302 };
  const currentLease = spawn(controller, replacement);
  assert.equal(controller.invalidate(oldLease), null);
  assert.equal(controller.currentChild(), replacement);
  assert.equal(controller.isCurrent(currentLease), true);
});

test("healthy backend becomes ready without an embedded runtime bridge", () => {
  const controller = createBackendLifecycleController();
  const lease = spawn(controller, { pid: 401 });
  assert.equal(controller.settleHealth(lease, true).state, "ready");
  assert.equal(controller.isReady(lease), true);
});

test("manual restart invalidates every callback owned by the stopped child", async () => {
  const controller = createBackendLifecycleController();
  const stoppedLease = spawn(controller, { pid: 501 });
  const stopped = controller.invalidate(stoppedLease);
  assert.equal(stopped.child.pid, 501);
  const replacementLease = spawn(controller, { pid: 502 });
  assert.equal(controller.settleHealth(stoppedLease, false).state, "stale");
  assert.equal(controller.isCurrent(stoppedLease), false);
  assert.equal(controller.isCurrent(replacementLease), true);
});

test("main lifecycle has no ownerless legacy state path", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  assert.doesNotMatch(
    source,
    /\bbackendProc\b|\bbackendWasReady\b|\bbackendLifecycleGeneration\b/,
  );
  const watcher = source
    .split("function startHealthWatcher", 2)[1]
    .split("function stopHealthWatcher", 1)[0];
  assert.match(watcher, /const ok = await healthcheckOnce\(\)/);
  assert.match(watcher, /backendLifecycle\.isCurrent\(lifecycleLease\)/);
  assert.doesNotMatch(source, /startFusedWorkerBridge|ECHODESK_RUNTIME_FD|ECHODESK_RUNTIME_NONCE/);
  const exitHandler = source
    .split('spawnedChild.on("exit"', 2)[1]
    .split("startHealthWatcher(lifecycleLease)", 1)[0];
  assert.match(exitHandler, /backendLifecycle\.isCurrent\(lifecycleLease\)/);
  assert.match(exitHandler, /handleBackendDeath\([^;]+lifecycleLease\)/s);
});

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createManualBackendRestart,
  stopBackendProcess,
  stopWindowsProcessTree,
} = require("../backend-manual-restart.cjs");

class FakeChild extends EventEmitter {
  exitCode = null;
  signals = [];

  kill(signal) {
    this.signals.push(signal);
    return true;
  }

  exit(code = 0) {
    this.exitCode = code;
    this.emit("exit", code, null);
  }
}

test("degraded packaged backend restart kills the child and reuses bundled-first spawn", async () => {
  const calls = [];
  let scheduled = null;
  const restart = createManualBackendRestart({
    isPublicDemo: () => false,
    healthcheckOnce: async () => false,
    emitStatus: (status) => calls.push(["status", status]),
    resetRestartState: () => calls.push(["reset"]),
    stopHealthWatcher: () => calls.push(["stop-health"]),
    stopExternalHealthWatcher: () => calls.push(["stop-external"]),
    stopBackendProc: async () => calls.push(["stop-bundled-child"]),
    spawnBackendAndWatch: () => calls.push(["spawn-bundled-first"]),
    isShuttingDown: () => false,
    schedule: (callback, delayMs) => {
      calls.push(["schedule", delayMs]);
      scheduled = callback;
    },
  });

  const running = restart();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [
    ["reset"],
    ["stop-health"],
    ["stop-external"],
    ["status", { state: "restarting", attempt: 1, backoff_ms: 500, reason: "manual restart" }],
    ["stop-bundled-child"],
    ["schedule", 500],
  ]);
  assert.equal(typeof scheduled, "function");
  scheduled();
  assert.deepEqual(await running, { ok: true, generation: 1 });
  assert.deepEqual(calls.at(-1), ["spawn-bundled-first"]);
});

test("manual restart is single-flight across double and triple clicks", async () => {
  let releaseStop;
  const stop = new Promise((resolve) => {
    releaseStop = resolve;
  });
  const calls = [];
  const scheduled = [];
  const restart = createManualBackendRestart({
    isPublicDemo: () => false,
    healthcheckOnce: async () => false,
    emitStatus: (status) => calls.push(["status", status]),
    resetRestartState: () => calls.push(["reset"]),
    stopHealthWatcher: () => calls.push(["stop-health"]),
    stopExternalHealthWatcher: () => calls.push(["stop-external"]),
    stopBackendProc: async () => {
      calls.push(["stop-child"]);
      await stop;
    },
    spawnBackendAndWatch: () => calls.push(["spawn"]),
    isShuttingDown: () => false,
    schedule: (callback) => scheduled.push(callback),
  });

  const first = restart();
  const second = restart();
  const third = restart();
  assert.equal(first, second);
  assert.equal(second, third);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.filter(([name]) => name === "stop-child").length, 1);
  releaseStop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  await Promise.all([first, second, third]);
  assert.equal(calls.filter(([name]) => name === "spawn").length, 1);
});

test("shutdown after a late child exit prevents the replacement spawn", async () => {
  let shuttingDown = false;
  let releaseStop;
  const stop = new Promise((resolve) => {
    releaseStop = resolve;
  });
  let spawns = 0;
  const restart = createManualBackendRestart({
    isPublicDemo: () => false,
    healthcheckOnce: async () => false,
    emitStatus: () => {},
    resetRestartState: () => {},
    stopHealthWatcher: () => {},
    stopExternalHealthWatcher: () => {},
    stopBackendProc: () => stop,
    spawnBackendAndWatch: () => {
      spawns += 1;
    },
    isShuttingDown: () => shuttingDown,
    schedule: (callback) => callback(),
  });
  const running = restart();
  shuttingDown = true;
  releaseStop();
  assert.deepEqual(await running, {
    ok: false,
    reason: "shutting-down",
    generation: 1,
  });
  assert.equal(spawns, 0);
});

test("backend stop waits for a late exit and cancels the stale SIGKILL timer", async () => {
  const child = new FakeChild();
  const timers = [];
  const cancelled = new Set();
  const stopped = stopBackendProcess(child, {
    schedule: (callback, delay) => {
      const timer = { callback, delay };
      timers.push(timer);
      return timer;
    },
    cancel: (timer) => cancelled.add(timer),
    graceMs: 500,
    killWaitMs: 100,
  });
  assert.deepEqual(child.signals, ["SIGTERM"]);
  assert.equal(timers.length, 1);
  child.exit();
  await stopped;
  assert.equal(cancelled.has(timers[0]), true);
  assert.deepEqual(child.signals, ["SIGTERM"]);
});

test("backend stop escalates once and rejects before any replacement can spawn", async () => {
  const child = new FakeChild();
  const timers = [];
  const stopped = stopBackendProcess(child, {
    schedule: (callback, delay) => {
      const timer = { callback, delay };
      timers.push(timer);
      return timer;
    },
    cancel: () => {},
    graceMs: 500,
    killWaitMs: 100,
  });
  timers[0].callback();
  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
  assert.equal(timers.length, 2);
  timers[1].callback();
  await assert.rejects(stopped, /did not exit after SIGKILL/);
  child.exit();
  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
});

test("Windows backend stop delegates to a complete process-tree terminator", async () => {
  const child = new FakeChild();
  child.pid = 1234;
  const calls = [];
  await stopBackendProcess(child, {
    platform: "win32",
    stopWindowsTree: async (proc) => calls.push(proc.pid),
  });
  assert.deepEqual(calls, [1234]);
  assert.deepEqual(child.signals, []);
});

test("Windows process-tree terminator waits for taskkill /T /F", async () => {
  const taskkill = new EventEmitter();
  const calls = [];
  const stopped = stopWindowsProcessTree(
    { pid: 4321, exitCode: null },
    {
      spawnProcess: (command, args, options) => {
        calls.push({ command, args, options });
        return taskkill;
      },
    },
  );
  assert.deepEqual(calls, [
    {
      command: "taskkill.exe",
      args: ["/PID", "4321", "/T", "/F"],
      options: { windowsHide: true, stdio: "ignore" },
    },
  ]);
  taskkill.emit("exit", 0);
  await stopped;
});

test("Windows process-tree terminator rejects an unverified taskkill failure", async () => {
  const taskkill = new EventEmitter();
  const child = { pid: 4321, exitCode: null };
  const stopped = stopWindowsProcessTree(
    child,
    { spawnProcess: () => taskkill },
  );
  // The PyInstaller bootloader can exit before its server child. A taskkill
  // failure must not be hidden merely because the directly spawned PID exited.
  child.exitCode = 0;
  taskkill.emit("exit", 1);
  await assert.rejects(stopped, /taskkill failed/);
});

test("manual restart source contract cannot resolve Python before supervisor selection", () => {
  const source = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  const handler = source
    .split('ipcMain.handle("backend:manual-restart"', 2)[1]
    .split("// ---------- app 生命周期 ----------", 1)[0];
  const spawn = source
    .split("function spawnBackendAndWatch()", 2)[1]
    .split("// ---------- 启动 ----------", 1)[0];

  assert.match(handler, /manualRestartBackend\(\)/);
  assert.doesNotMatch(handler, /resolvePython|pythonResolved/);
  assert.match(source, /stopBackendProcForRestart/);
  assert.match(source, /backendLifecycleGeneration/);
  assert.match(spawn, /supervised child is already running/);
  assert.ok(spawn.indexOf("bundledBackendExecutable()") < spawn.indexOf("resolvePython()"));
});

test("application shutdown waits for the shared backend process-tree stop", () => {
  const source = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  const handler = source.split('app.on("before-quit"', 2)[1];
  assert.match(handler, /stopBackendProcess\(proc, \{ graceMs: SIGKILL_GRACE_MS \}\)/);
  assert.doesNotMatch(handler, /proc\.kill\(/);
});

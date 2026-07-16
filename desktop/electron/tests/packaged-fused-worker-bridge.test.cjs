const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const bridge = require("../packaged-fused-worker-bridge.cjs");

const root = path.resolve(__dirname, "..");
const mainSource = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
const bridgeSource = fs.readFileSync(path.join(root, "packaged-fused-worker-bridge.cjs"), "utf8");

test("packaged main has the executable fused-worker lifecycle wiring", () => {
  assert.match(mainSource, /startPackagedFusedWorkerBridge/);
  assert.match(mainSource, /ECHODESK_RUNTIME_FD:\s*"3"/);
  assert.match(mainSource, /ECHODESK_RUNTIME_NONCE/);
  assert.match(mainSource, /stdio:\s*\["ignore", "pipe", "pipe", "pipe"\]/);
  assert.match(mainSource, /startFusedWorkerBridge\(\)/);
  assert.match(mainSource, /stopFusedWorkerBridge\(\)/);
  assert.match(bridgeSource, /new Worker\(/);
  assert.match(bridgeSource, /workerData:/);
  assert.match(bridgeSource, /requestWorker\(/);
  assert.match(bridgeSource, /runTurn\(/);
  assert.match(bridgeSource, /task\.event/);
  assert.match(bridgeSource, /runtime\.host\.request/);
});

test("packaged fused worker path fails closed when the re-bound runtime manifest is absent", () => {
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-b13-runtime-"));
  try {
    const duplex = {
      on() { return this; },
      write() { return true; },
      destroy() {},
    };
    assert.throws(
      () => bridge.startPackagedFusedWorkerBridge({
        duplex,
        nonce: "test-runtime-nonce",
        resourcesPath,
      }),
      (error) => error instanceof bridge.PackagedFusedWorkerError && error.code === "PACKAGE_MANIFEST_MISSING",
    );
  } finally {
    fs.rmSync(resourcesPath, { recursive: true, force: true });
  }
});

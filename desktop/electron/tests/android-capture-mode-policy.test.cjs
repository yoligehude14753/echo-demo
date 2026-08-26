"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
const vm = require("node:vm");

function loadPolicy() {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../../src/capture/captureModePolicy.ts"),
    "utf8",
  );
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(output, {
    module,
    exports: module.exports,
    require,
    Set,
    Error,
  });
  return module.exports;
}

test("Android capture selector only offers online devices", () => {
  const { onlineCaptureDevices } = loadPolicy();
  assert.deepEqual(
    Array.from(
      onlineCaptureDevices([
        { deviceId: "android", online: true },
        { deviceId: "desktop", online: false },
      ]),
      (device) => device.deviceId,
    ),
    ["android"],
  );
});

test("single mode requires exactly one selected device", () => {
  const { buildCaptureControlUpdate } = loadPolicy();
  assert.throws(() => buildCaptureControlUpdate("single", [], 3), /只能选择一台/);
  assert.throws(
    () => buildCaptureControlUpdate("single", ["a", "b"], 3),
    /只能选择一台/,
  );
  assert.deepEqual(
    JSON.parse(
      JSON.stringify(buildCaptureControlUpdate("single", ["android"], 3)),
    ),
    {
      mode: "single",
      selectedDeviceIds: ["android"],
      expectedRevision: 3,
    },
  );
});

test("multi mode de-duplicates selected devices and preserves revision", () => {
  const { buildCaptureControlUpdate, isDeviceSelected } = loadPolicy();
  const update = buildCaptureControlUpdate("multi", ["android", "desktop", "android"], 8);
  assert.deepEqual(Array.from(update.selectedDeviceIds), ["android", "desktop"]);
  assert.equal(update.expectedRevision, 8);
  assert.equal(
    isDeviceSelected(
      { mode: "multi", selectedDeviceIds: update.selectedDeviceIds, revision: 9 },
      "desktop",
    ),
    true,
  );
});

test("invalid revisions fail closed before control update", () => {
  const { buildCaptureControlUpdate } = loadPolicy();
  assert.throws(
    () => buildCaptureControlUpdate("single", ["android"], -1),
    /非负整数/,
  );
});

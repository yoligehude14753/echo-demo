"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const runtime = readFileSync(path.resolve(__dirname, "../../src/runtime.ts"), "utf8");
const settings = readFileSync(
  path.resolve(__dirname, "../../src/components/SettingsPanel.tsx"),
  "utf8",
);

test("packaged renderer prefers main-process backend authority over localStorage", () => {
  const snapshot = runtime.slice(
    runtime.indexOf("export function backendBaseSnapshot"),
    runtime.indexOf("export function isDefaultPublicBackend"),
  );
  assert.ok(
    snapshot.indexOf("if (isPackagedElectronRenderer())") <
      snapshot.indexOf("const configured = configuredBackendBase()"),
  );
  assert.match(
    runtime,
    /setStoredBackendBase[\s\S]+?if \(isPackagedElectronRenderer\(\)\)[\s\S]+?window\.echo\?\.backendHost/,
  );
});

test("installed Settings hides the mobile backend-origin editor", () => {
  assert.match(settings, /backendOriginEditable = !isPackagedElectronRenderer\(\)/);
  assert.match(
    settings,
    /\{backendOriginEditable && \([\s\S]+?<span>移动端连接<\/span>[\s\S]+?mobile-backend-base/,
  );
  assert.match(settings, /if \(!backendOriginEditable\) return;/);
});

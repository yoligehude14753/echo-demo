"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const mainSource = fs.readFileSync(
  path.resolve(__dirname, "../main.cjs"),
  "utf8",
);

test("user-facing window creation has a post-load visibility fallback", () => {
  const loadBoundary = mainSource.indexOf("await mainWindow.loadURL(APP_ENTRY_URL)");
  const fallback = mainSource.indexOf(
    "if (showOnReady && mainWindow && !mainWindow.isDestroyed())",
    loadBoundary,
  );
  const lifecycleMarker = mainSource.indexOf(
    'emitBootStage("windowCreated")',
    loadBoundary,
  );

  assert.ok(loadBoundary >= 0, "packaged renderer load must remain explicit");
  assert.ok(fallback > loadBoundary, "fallback must run after trusted navigation");
  assert.ok(fallback < lifecycleMarker, "fallback must precede window-created bookkeeping");
  assert.match(
    mainSource.slice(fallback, lifecycleMarker),
    /showMainWindow\(\);/,
  );
});

test("installed startup owns a local runtime endpoint when LaunchServices supplies none", () => {
  const resolveIndex = mainSource.indexOf("resolveBackendEndpoint(backendConfig");
  const ownerIndex = mainSource.indexOf("function lifecycleEnvironment");
  const allocateIndex = mainSource.indexOf("function allocateLifecycleEndpoint");

  assert.ok(ownerIndex >= 0);
  assert.ok(allocateIndex >= 0);
  assert.ok(resolveIndex > ownerIndex);
  assert.match(mainSource.slice(Math.min(ownerIndex, allocateIndex), resolveIndex), /ECHODESK_BASE_URL/);
  assert.match(mainSource, /runtime[\s\S]{0,80}endpoint\.json/);
});

test("activate and second-instance paths restore the main window", () => {
  const activate = mainSource.indexOf('app.on("activate"');
  const secondInstance = mainSource.indexOf('app.on("second-instance"');

  assert.ok(activate >= 0);
  assert.ok(secondInstance > activate);
  assert.match(
    mainSource.slice(activate, secondInstance),
    /BrowserWindow\.getAllWindows\(\)\.length === 0[\s\S]*createWindow\(\{ showOnReady: true \}/,
  );
  assert.match(
    mainSource.slice(activate, secondInstance),
    /else \{[\s\S]*showMainWindow\(\);/,
  );
  assert.match(
    mainSource.slice(secondInstance, secondInstance + 120),
    /showMainWindow\(\);/,
  );
});

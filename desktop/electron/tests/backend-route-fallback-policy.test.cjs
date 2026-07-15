"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const main = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
const runtime = readFileSync(path.resolve(__dirname, "../../src/runtime.ts"), "utf8");
const restart = readFileSync(
  path.resolve(__dirname, "../backend-manual-restart.cjs"),
  "utf8",
);

function mainBackendSelectionBlock() {
  return main
    .split("function projectRoot()", 2)[1]
    .split("// ---------- 端口探测 ----------", 2)[0];
}

function mainLifecycleBlock() {
  return main
    .split("function stopHealthWatcher()", 2)[1]
    .split("// ---------- 进程生命周期 ----------", 2)[0];
}

test("local Electron backend refuses unmanaged daemons and external runtime discovery", () => {
  const selection = mainBackendSelectionBlock();
  assert.match(selection, /refusePackagedSourceFallback/);
  assert.match(selection, /process\.env\.ECHO_PYTHON/);
  assert.match(selection, /path\.isAbsolute\(explicit\)/);
  assert.doesNotMatch(selection, /os\.homedir\(\)/);
  assert.doesNotMatch(selection, /\/usr\/bin\/python3|["']python3["']/);
  assert.doesNotMatch(selection, /process\.env\.PATH/);
  assert.doesNotMatch(selection, /ECHO_ALLOW_PACKAGED_SOURCE_BACKEND/);
  assert.doesNotMatch(selection, /attachExternalBackend|external backend/);
  assert.match(main, /backend-port-conflict/);
  assert.match(main, /backend-spawn-disabled/);
});

test("public service is the only non-supervised backend route", () => {
  const lifecycle = mainLifecycleBlock();
  assert.match(lifecycle, /startPublicBackendHealthWatcher/);
  assert.match(lifecycle, /attachPublicBackend/);
  assert.doesNotMatch(lifecycle, /externalMode|externalBackend|startExternal|stopExternal/);
  assert.match(main, /if \(PUBLIC_DEMO_MODE\) \{/);
  assert.match(main, /connecting to public service/);
});

test("mobile route cannot fall back to a relative WebView proxy", () => {
  const route = runtime
    .split("export function mobilePcBackendBase", 2)[1]
    .split("export async function checkAppUpdate", 2)[0];
  assert.match(route, /configuredMobilePcBackendBase\(\)/);
  assert.match(route, /mobile_backend_route_unavailable/);
  assert.doesNotMatch(route, /window\.location|apiPath\(|canUseRelativeBackendProxy\(/);
  assert.match(runtime, /if \(isNativeMobile\(\)\) return mobilePcBackendBase\(\);/);
});

test("manual restart only stops the owned child and public route watcher", () => {
  assert.match(restart, /stopPublicBackendHealthWatcher/);
  assert.doesNotMatch(restart, /stopExternalHealthWatcher|attachExternalBackend/);
});

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
    snapshot.indexOf("hasElectronBackendRouting()") <
      snapshot.indexOf("const configured = configuredBackendBase()"),
  );
  assert.match(runtime, /setStoredBackendBase/);
  assert.match(runtime, /hasElectronBackendRouting/);
  assert.match(runtime, /window\.echo\?\.backendHost/);
});

test("artifact URL policy has no local fallback", () => {
  const artifactApi = readFileSync(
    path.resolve(__dirname, "../../src/api.ts"),
    "utf8",
  );
  const artifactFunction = artifactApi
    .split("export function artifactDownloadUrl", 2)[1]
    .split("\n}\n", 2)[0];
  assert.doesNotMatch(artifactFunction, /DEFAULT_LOCAL_BACKEND_BASE|localhost|127\.0\.0\.1/);
  assert.match(artifactFunction, /backendBaseSnapshot\(\)/);
  assert.match(artifactFunction, /artifact_backend_snapshot_missing/);
});

test("public endpoint absence cannot use the relative development proxy", () => {
  assert.match(runtime, /function canUseRelativeBackendProxy\(\)/);
  const wsFunction = runtime
    .split("export async function backendWsUrl", 2)[1]
    .split("export function apiPath", 2)[0];
  const apiFunction = runtime
    .split("export async function apiUrl", 2)[1]
    .split("\n}", 2)[0];
  const artifactApi = readFileSync(
    path.resolve(__dirname, "../../src/api.ts"),
    "utf8",
  );
  const artifactFunction = artifactApi
    .split("export function artifactDownloadUrl", 2)[1]
    .split("\n}\n", 2)[0];
  assert.match(wsFunction, /canUseRelativeBackendProxy\(\)/);
  assert.match(apiFunction, /canUseRelativeBackendProxy\(\)/);
  assert.match(artifactFunction, /canUseRelativeBackendProxy\(\)/);
});

test("invalid transcription reason codes map to unknown", () => {
  const session = readFileSync(
    path.resolve(__dirname, "../../src/session.ts"),
    "utf8",
  );
  const reasonValidation = session
    .split('"reason_code" in value', 2)[1]
    .split('if (\n    "retry_after_s"', 2)[0];
  assert.match(
    reasonValidation,
    /return \{ status: "unknown", diagnostic: "readiness_unknown_malformed" \}/,
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

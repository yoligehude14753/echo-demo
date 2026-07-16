"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const main = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
const preload = readFileSync(path.resolve(__dirname, "../preload.cjs"), "utf8");
const updater = readFileSync(
  path.resolve(__dirname, "../app-update-protocol.cjs"),
  "utf8",
);
const modelRuntimeContract = readFileSync(
  path.resolve(__dirname, "../model-runtime-contract.cjs"),
  "utf8",
);
const preview = readFileSync(
  path.resolve(__dirname, "../../src/components/ArtifactPreviewModal.tsx"),
  "utf8",
);

function registrationBody(channel) {
  const handleMarker = `ipcMain.handle("${channel}"`;
  const onMarker = `ipcMain.on("${channel}"`;
  const start = Math.max(main.indexOf(handleMarker), main.indexOf(onMarker));
  assert.notEqual(start, -1, `missing IPC registration for ${channel}`);
  const nextHandle = main.indexOf("ipcMain.handle(\"", start + 1);
  const nextOn = main.indexOf("ipcMain.on(\"", start + 1);
  const candidates = [nextHandle, nextOn].filter((value) => value !== -1);
  const end = candidates.length > 0 ? Math.min(...candidates) : main.length;
  return main.slice(start, end);
}

test("BrowserWindow keeps the preload bridge sandboxed", () => {
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
  assert.match(main, /sandbox:\s*true/);
  assert.doesNotMatch(main, /sandbox:\s*false/);
});

test("every renderer-callable IPC channel starts with a trusted-origin guard", () => {
  for (const channel of [
    "echo:backend-host",
    "echo:share-backend-host",
    "echo:backend-host-sync",
    "echo:is-public-demo",
    "credential:ensure-session",
    "credential:renew-session",
    "credential:rotate",
    "credential:clear-public",
    "echo:load-local-legacy-history",
    "shell:open-external",
    "updates:check",
    "updates:last-status",
    "updates:download-and-install",
    "updates:open-release",
    "mic:status",
    "mic:request",
    "echo:open-artifact-in-system",
    "echo:download-renderer-blob",
    "workspace:pick-directory",
    "workspace:local-status",
    "workspace:add-local-dir",
    "workspace:remove-local-dir",
    "workspace:scan-local",
    "workspace:clear-local-docs",
    "workspace:cancel-origin-operations",
    "mic:open-system-prefs",
    "backend:manual-restart",
  ]) {
    const body = registrationBody(channel);
    const guard = body.indexOf("assertTrustedIpcOrigin(event)");
    assert.ok(guard >= 0 && guard < 300, `${channel} must guard before work`);
  }
});

test("model runtime identity/fallback bridges are read-only subscriptions", () => {
  assert.match(preload, /getModelRuntimeIdentity:\s*\(\) => ipcRenderer\.invoke\("model-runtime:get-identity"\)/);
  assert.match(preload, /onModelRuntimeIdentity:[\s\S]*model-runtime:identity/);
  assert.match(preload, /onModelRuntimeFallback:[\s\S]*model-runtime:fallback/);
  assert.doesNotMatch(preload, /publishModelRuntimeIdentity/);
  assert.doesNotMatch(preload, /setModelRuntimeIdentity/);
  assert.match(main, /modelRuntimeIpc\.register\(\)/);
  assert.match(modelRuntimeContract, /ipcMain\.handle\("model-runtime:get-identity"/);
  assert.match(modelRuntimeContract, /assertTrustedIpcOrigin\(event\)/);
});

test("workspace picker exposes local paths only outside public mode", () => {
  const body = registrationBody("workspace:pick-directory");
  const localReturn = body.indexOf("if (!PUBLIC_DEMO_MODE) return selectedPath");
  const publicHandle = body.indexOf(
    "const handle = workspaceHandle(expectedOrigin, selectedPath)",
  );
  const pendingSelection = body.indexOf("pendingWorkspaceSelections.set");
  assert.ok(localReturn >= 0, "local mode must return the native selected path");
  assert.ok(
    localReturn < publicHandle && publicHandle < pendingSelection,
    "public mode must convert the path to an origin-bound pending handle",
  );
});

test("renderer blob downloads stay origin-bound, bounded, and main-owned", () => {
  const body = registrationBody("echo:download-renderer-blob");
  const guard = body.indexOf("assertTrustedIpcOrigin(event)");
  const policy = body.indexOf("downloadRendererBlob({");
  assert.ok(guard >= 0 && guard < policy);
  assert.match(body, /senderFrame: event\.senderFrame/);
  assert.match(body, /downloadDirectory: app\.getPath\("downloads"\)/);
  assert.match(body, /activeArtifactDownloadSenders\.has\(sender\)/);
  assert.doesNotMatch(body, /authorization|bearer|token|downloadDirectory[,)]/i);
});

test("local artifact open is denied remotely and resolved inside controlled roots before OS handoff", () => {
  const body = registrationBody("echo:open-artifact-in-system");
  const guard = body.indexOf("assertTrustedIpcOrigin(event)");
  const publicGate = body.indexOf("if (PUBLIC_DEMO_MODE)");
  const resolve = body.indexOf("resolveControlledLocalArtifactPath(");
  const open = body.indexOf("shell.openPath(controlledPath)");
  assert.ok(guard >= 0 && guard < publicGate);
  assert.ok(publicGate < resolve && resolve < open);
  assert.doesNotMatch(body, /openPath failed \(\$\{filePath\}\)/);
  assert.match(preview, /bridge\.isPublicDemo !== true/);
  assert.match(preview, /apiTransport\([\s\S]*downloadUrl/);
  assert.match(preview, /a\.href = objectUrl/);
  assert.match(preview, /window\.echo\?\.isPublicDemo !== true/);
});

test("external URL and legacy history bridges do not expose broad local capabilities", () => {
  assert.match(registrationBody("shell:open-external"), /openExternalHttps\(url\)/);
  assert.match(main, /target\.protocol !== "https:"/);
  assert.doesNotMatch(main, /sourcePath:\s*dbPath/);
  assert.doesNotMatch(registrationBody("echo:load-local-legacy-history"), /e\?\.message/);
});

test("update and microphone IPC expose stable safe errors instead of local paths", () => {
  assert.doesNotMatch(
    registrationBody("updates:download-and-install"),
    /e\?\.message|String\(e\)/,
  );
  assert.match(main, /safeUpdateFailure\(/);
  assert.match(
    registrationBody("mic:open-system-prefs"),
    /reason: "system-preferences-open-failed"/,
  );
  assert.doesNotMatch(registrationBody("mic:open-system-prefs"), /e\?\.message/);
});

test("release metadata and packages are bounded and digest-verified", () => {
  assert.match(updater, /fetchBoundedHttpsJson\(apiUrl, \{/);
  assert.match(updater, /maxBytes: MAX_RELEASES_BYTES/);
  assert.match(updater, /timeoutMs: 8_000/);
  assert.match(updater, /validate: isReleaseList/);
  assert.match(updater, /normalizeDigest\(asset\.digest\)/);
  assert.match(updater, /hash\.digest\("hex"\) !== expectedDigest/);
  assert.doesNotMatch(main, /electron-updater|quitAndInstall/);
});

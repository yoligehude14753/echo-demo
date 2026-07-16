"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  makeManifest,
  normalizeResourcePath,
  sha256File,
  swapResources,
  validateManifest,
  verifyInstalledBase,
  writePatch,
} = require("./desktop-resource-hotpatch.cjs");
const {
  createStoreZip,
  extractStoreZip,
  readCentralDirectory,
} = require("./lib/store-zip.cjs");

const FROM_SHA = "1".repeat(40);
const TO_SHA = "2".repeat(40);

function temporaryRoot(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-hotpatch-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function write(root, relativePath, contents) {
  const target = path.join(root, ...relativePath.split("/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, contents);
  return target;
}

test("manifest binds old and new hashes and only selects allowed resource roots", (t) => {
  const root = temporaryRoot(t);
  const base = path.join(root, "base");
  const next = path.join(root, "next");
  write(base, "app.asar", "old-asar");
  write(base, "agent-runtime/worker.mjs", "old-worker");
  write(base, "agent-runtime/stale.mjs", "stale");
  write(next, "app.asar", "new-asar");
  write(next, "agent-runtime/worker.mjs", "new-worker");
  write(next, "agent-runtime/new.mjs", "new");
  write(next, "backend/echodesk-backend", "not-selected");

  const manifest = makeManifest({
    baseResources: base,
    nextResources: next,
    includes: ["app.asar", "agent-runtime"],
    fromSource: FROM_SHA,
    fromVersion: "0.3.3-preview.3",
    toSource: TO_SHA,
  });

  assert.deepEqual(manifest.files.map((file) => [file.path, file.operation]), [
    ["agent-runtime/new.mjs", "put"],
    ["agent-runtime/stale.mjs", "delete"],
    ["agent-runtime/worker.mjs", "put"],
    ["app.asar", "put"],
  ]);
  assert.equal(manifest.files.some((file) => file.path.startsWith("backend/")), false);
  assert.equal(manifest.files.find((file) => file.path === "app.asar").from_sha256, sha256File(path.join(base, "app.asar")));
});

test("forbidden executables, helpers, DLLs and traversal paths fail closed", () => {
  for (const unsafe of [
    "../resources/app.asar",
    "/resources/app.asar",
    "C:/EchoDesk/EchoDesk.exe",
    "EchoDesk.exe",
    "resources/app.asar",
    "app.asar/electron/main.cjs",
    "backend\\evil.dll",
  ]) {
    assert.throws(() => normalizeResourcePath(unsafe), /unsafe|allowlist|one file/);
  }
});

test("patch directory and stored ZIP preserve the hash-bound payload", (t) => {
  const root = temporaryRoot(t);
  const base = path.join(root, "base");
  const next = path.join(root, "next");
  const patch = path.join(root, "patch");
  const zip = path.join(root, "patch.zip");
  const extracted = path.join(root, "extracted");
  write(base, "app.asar", "before");
  write(next, "app.asar", "after");
  const manifest = makeManifest({
    baseResources: base,
    nextResources: next,
    includes: ["app.asar"],
    fromSource: FROM_SHA,
    fromVersion: "0.3.3-preview.3",
    toSource: TO_SHA,
  });

  writePatch({ manifest, nextResources: next, outputDirectory: patch, zipPath: zip });
  assert.deepEqual(readCentralDirectory(zip).map((entry) => entry.name).sort(), [
    "manifest.json",
    "manifest.sha256",
    "payload/app.asar",
  ]);
  extractStoreZip(zip, extracted);
  validateManifest(JSON.parse(fs.readFileSync(path.join(extracted, "manifest.json"), "utf8")), extracted);
  assert.equal(fs.readFileSync(path.join(extracted, "payload", "app.asar"), "utf8"), "after");
});

test("ZIP writer rejects unsafe entry names before creating an archive", (t) => {
  const root = temporaryRoot(t);
  const payload = write(root, "payload.txt", "payload");
  assert.throws(
    () => createStoreZip([{ name: "../outside", source: payload }], path.join(root, "unsafe.zip")),
    /unsafe archive path/,
  );
});

test("apply refuses an installed tree whose old hash does not match", (t) => {
  const root = temporaryRoot(t);
  const base = path.join(root, "base");
  const next = path.join(root, "next");
  write(base, "app.asar", "expected-before");
  write(next, "app.asar", "after");
  const manifest = makeManifest({
    baseResources: base,
    nextResources: next,
    includes: ["app.asar"],
    fromSource: FROM_SHA,
    fromVersion: "0.3.3-preview.3",
    toSource: TO_SHA,
  });
  write(base, "app.asar", "locally-modified");
  assert.throws(() => verifyInstalledBase(base, manifest), /installed base hash mismatch/);
});

test("macOS signing failure restores the original Resources tree", (t) => {
  const root = temporaryRoot(t);
  const app = path.join(root, "EchoDesk.app");
  const resources = path.join(app, "Contents", "Resources");
  const patch = path.join(root, "patch");
  write(resources, "app.asar", "old");
  write(patch, "payload/app.asar", "new");
  const manifest = {
    to: { source_sha: TO_SHA },
    files: [{
      path: "app.asar",
      operation: "put",
      from_sha256: sha256File(path.join(resources, "app.asar")),
      sha256: sha256File(path.join(patch, "payload", "app.asar")),
      size: 3,
    }],
  };
  let calls = 0;
  const commandRunner = () => {
    calls += 1;
    if (calls === 1) throw new Error("simulated codesign failure");
  };

  assert.throws(
    () => swapResources({
      resources,
      patchRoot: patch,
      manifest,
      platform: "darwin",
      appPath: app,
      keepBackup: false,
      commandRunner,
    }),
    /simulated codesign failure/,
  );
  assert.equal(fs.readFileSync(path.join(resources, "app.asar"), "utf8"), "old");
  assert.equal(fs.readdirSync(path.dirname(resources)).some((name) => name.includes(".echodesk-resources-")), false);
});

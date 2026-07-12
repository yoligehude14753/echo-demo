const assert = require("node:assert/strict");
const {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { peMachine } = require("../../scripts/build-backend-win.cjs");
const {
  verifyFrozenAnalysis,
} = require("../../scripts/backend-frozen-contract.cjs");
const {
  verifyBundledBackend,
} = require("../../scripts/verify-bundled-backend.cjs");

function writeX64Pe(target) {
  const bytes = Buffer.alloc(256);
  bytes.writeUInt16LE(0x5a4d, 0);
  bytes.writeUInt32LE(128, 0x3c);
  bytes.writeUInt32LE(0x00004550, 128);
  bytes.writeUInt16LE(0x8664, 132);
  writeFileSync(target, bytes);
}

test("Windows backend builder verifies the x64 PE machine header", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-pe-contract-"));
  const executable = path.join(root, "backend.exe");
  writeX64Pe(executable);
  try {
    assert.equal(peMachine(executable), 0x8664);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("electron-builder refuses Windows packaging without backend spec", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-release-contract-"));
  const dist = path.join(root, "backend", "dist");
  mkdirSync(dist, { recursive: true });
  writeX64Pe(path.join(dist, "echodesk-backend.exe"));
  try {
    assert.throws(
      () => verifyBundledBackend({ platform: "win32", repoRoot: root }),
      /missing .*echodesk-backend\.spec/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("electron-builder refuses Windows packaging without backend executable", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-release-contract-"));
  const packaging = path.join(root, "backend", "packaging");
  mkdirSync(packaging, { recursive: true });
  writeFileSync(path.join(packaging, "echodesk-backend.spec"), "# test spec\n");
  try {
    assert.throws(
      () => verifyBundledBackend({ platform: "win32", repoRoot: root }),
      /missing backend artifact/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("electron-builder accepts only a complete Windows backend contract", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-release-contract-"));
  const packaging = path.join(root, "backend", "packaging");
  const dist = path.join(root, "backend", "dist");
  mkdirSync(packaging, { recursive: true });
  mkdirSync(dist, { recursive: true });
  writeFileSync(path.join(packaging, "echodesk-backend.spec"), "# test spec\n");
  const executable = path.join(dist, "echodesk-backend.exe");
  writeX64Pe(executable);
  try {
    assert.equal(
      verifyBundledBackend({ platform: "win32", repoRoot: root }),
      executable,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("package entry points cannot bypass the bundled backend verifier", () => {
  const pkg = require(path.resolve(__dirname, "../../package.json"));
  assert.equal(pkg.build.beforePack, "scripts/verify-bundled-backend.cjs");
  assert.equal(
    pkg.scripts["app:dist:win"],
    "node scripts/desktop-release-signing.cjs windows",
  );
  assert.match(
    pkg.scripts["app:dist:win:unsigned-test"],
    /^npm run backend:build:win/,
  );
});

test("frozen backend manifest rejects unused optional audio runtimes", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-frozen-contract-"));
  const manifest = path.join(root, "Analysis-00.toc");
  try {
    writeFileSync(
      manifest,
      "[['speech_recognition'], ('huggingface_hub.inference.automatic_speech_recognition', '/tmp/hf.py', 'PYMODULE'), ('app.main', '/tmp/app/main.py', 'PYMODULE')]\n",
    );
    assert.equal(verifyFrozenAnalysis(manifest), true);
    writeFileSync(
      manifest,
      "[('speech_recognition/flac-mac', '/tmp/flac-mac', 'BINARY')]\n",
    );
    assert.throws(
      () => verifyFrozenAnalysis(manifest),
      /forbidden optional audio runtime.*speech_recognition.*flac-mac/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

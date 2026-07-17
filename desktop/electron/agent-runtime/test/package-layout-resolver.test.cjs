const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  PackageLayoutError,
  normalizeResourcePath,
  resolvePackageResource,
} = require("../package-layout-resolver.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-b12-layout-"));
  const resources = path.join(root, "Resources");
  fs.mkdirSync(resources, { recursive: true });
  return { root, resources };
}

function cleanup(root) {
  fs.rmSync(root, { recursive: true, force: true });
}

function writePackageFile(resources, placement, relativePath, content) {
  const base = placement === "asar"
    ? path.join(resources, "app.asar")
    : placement === "asarUnpack"
      ? path.join(resources, "app.asar.unpacked")
      : resources;
  const target = path.join(base, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
  return target;
}

function entry(packagePath, placement, content) {
  return {
    path: packagePath,
    placement,
    size: content.length,
    sha256: crypto.createHash("sha256").update(content).digest("hex"),
    executable: false,
    role: "test-resource",
    platform: "darwin|win32",
    arch: "arm64|x64",
  };
}

test("resolves asar, app.asar.unpacked and extraResources from Resources only", () => {
  const { root, resources } = fixture();
  try {
    const asarBytes = Buffer.from("asar-resource");
    const unpackedBytes = Buffer.from("unpacked-resource");
    const extraBytes = Buffer.from("extra-resource");
    const asarPath = "app.asar/electron/main.cjs";
    const unpackedPath = "Resources/agent-runtime/worker.mjs";
    const extraPath = "Resources/backend/echodesk-backend";
    writePackageFile(resources, "asar", "electron/main.cjs", asarBytes);
    writePackageFile(resources, "asarUnpack", "agent-runtime/worker.mjs", unpackedBytes);
    writePackageFile(resources, "extraResources", "backend/echodesk-backend", extraBytes);

    const asar = resolvePackageResource(entry(asarPath, "asar", asarBytes), { resourcesPath: root });
    const unpacked = resolvePackageResource(entry(unpackedPath, "asarUnpack", unpackedBytes), { resourcesPath: root });
    const extra = resolvePackageResource(entry(extraPath, "extraResources", extraBytes), { resourcesPath: root });

    assert.equal(asar.path, "electron/main.cjs");
    assert.equal(unpacked.path, "agent-runtime/worker.mjs");
    assert.equal(extra.path, "backend/echodesk-backend");
    assert.equal(asar.size, asarBytes.length);
    assert.match(unpacked.sha256, /^sha256:[0-9a-f]{64}$/);
    assert.equal(extra.placement, "extraResources");
  } finally {
    cleanup(root);
  }
});

test("rejects absolute, parent, separator and invalid placement paths", () => {
  for (const value of ["/tmp/escape", "C:/escape", "\\\\server\\share\\escape", "../escape", "a/../escape", "a\\b"]) {
    assert.throws(() => normalizeResourcePath(value, "extraResources"), (error) => {
      assert.ok(error instanceof PackageLayoutError);
      assert.equal(error.code, "PACKAGE_RESOURCE_PATH_INVALID");
      return true;
    });
  }
  assert.throws(
    () => normalizeResourcePath("agent-runtime/worker.mjs", "cwd"),
    (error) => error.code === "PACKAGE_RESOURCE_PLACEMENT_INVALID",
  );
});

test("fails closed on size, hash, symlink and ambiguous auto resolution", () => {
  const { root, resources } = fixture();
  try {
    const bytes = Buffer.from("verified-resource");
    writePackageFile(resources, "asarUnpack", "agent-runtime/worker.mjs", bytes);
    const valid = entry("agent-runtime/worker.mjs", "asarUnpack", bytes);

    assert.throws(
      () => resolvePackageResource({ ...valid, size: valid.size + 1 }, { resourcesPath: root }),
      (error) => error.code === "PACKAGE_RESOURCE_SIZE_MISMATCH",
    );
    assert.throws(
      () => resolvePackageResource({ ...valid, sha256: "0".repeat(64) }, { resourcesPath: root }),
      (error) => error.code === "PACKAGE_RESOURCE_HASH_MISMATCH",
    );

    const real = writePackageFile(resources, "asarUnpack", "agent-runtime/real.mjs", bytes);
    fs.symlinkSync(real, path.join(resources, "app.asar.unpacked", "agent-runtime", "link.mjs"));
    assert.throws(
      () => resolvePackageResource(entry("agent-runtime/link.mjs", "asarUnpack", bytes), { resourcesPath: root }),
      (error) => error.code === "PACKAGE_RESOURCE_SYMLINK",
    );

    writePackageFile(resources, "asar", "ambiguous.mjs", bytes);
    writePackageFile(resources, "extraResources", "ambiguous.mjs", bytes);
    assert.throws(
      () => resolvePackageResource(entry("ambiguous.mjs", "auto", bytes), { resourcesPath: root }),
      (error) => error.code === "PACKAGE_RESOURCE_AMBIGUOUS",
    );
  } finally {
    cleanup(root);
  }
});

test("resolver source has no cwd, environment, home or path discovery surface", () => {
  const source = fs.readFileSync(require.resolve("../package-layout-resolver.cjs"), "utf8");
  assert.doesNotMatch(source, /process\.cwd\s*\(/);
  assert.doesNotMatch(source, /process\.env/);
  assert.doesNotMatch(source, /os\.homedir\s*\(/);
  assert.doesNotMatch(source, /\b(?:HOME|PATH)\b/);
});

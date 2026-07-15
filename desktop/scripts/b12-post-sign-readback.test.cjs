const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const {
  mkdtempSync,
  mkdirSync,
  copyFileSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const {
  computeCanonicalManifestDigest,
  comparePeCoffTrees,
  enumeratePeCoffFiles,
  readbackWindowsPeScope,
  readback,
  validateManifest,
} = require("./b12-post-sign-readback.cjs");

const RELEASE_SHA = "ffbacb9d0ffa1b62a205f98ff437be4219e9ee08";

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-b12-readback-test-"));
  const runtimeRoot = path.join(root, "Resources", "agent-runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const worker = Buffer.from("unsigned worker fixture\n");
  const workerPath = path.join(runtimeRoot, "worker.mjs");
  writeFileSync(workerPath, worker);
  const completedEntry = {
    package_relative_path: "Resources/agent-runtime/worker.mjs",
    size_bytes: worker.length,
    sha256: createHash("sha256").update(worker).digest("hex"),
    role: "electron_worker_entry",
    platform: "darwin|win32",
    arch: "arm64|x64",
    executable: true,
    placement: "extraResources",
    status: "unsigned_fixture",
  };
  const manifest = {
    schema_version: 1,
    manifest_type: "echo.b12.fusion-content",
    release_sha: RELEASE_SHA,
    content_entries: [
      completedEntry,
      {
        package_relative_path: "Resources/backend/echodesk-backend.exe",
        size_bytes: null,
        sha256: null,
        role: "bundled_backend_executable",
        platform: "win32",
        arch: "x64",
        executable: true,
        placement: "extraResources",
        status: "pending_unsigned_build",
      },
    ],
    forbidden_fallback_scan: { status: "pass", findings: [] },
    manifest_digest: {
      algorithm: "sha256",
      value: "PENDING_CANONICAL_DIGEST",
    },
  };
  writeFileSync(
    path.join(runtimeRoot, "fusion-content-manifest.json"),
    JSON.stringify(manifest, null, 2),
  );
  return { root, runtimeRoot, workerPath, manifest, completedEntry };
}

test("reads canonical content_entries/size_bytes and preserves pending entries", () => {
  const current = fixture();
  try {
    const result = readback({ layoutRoot: current.root, expectedReleaseSha: RELEASE_SHA });
    assert.equal(result.status, "PASS");
    assert.equal(result.verdict, "post_sign_readback_pass");
    assert.equal(result.checked_files.length, 1);
    assert.equal(result.pending_files.length, 1);
    assert.equal(result.manifest_digest_status, "pending");
    assert.equal(result.signature_validation.executed, false);
    assert.equal(result.signature_validation.signed_state_asserted, false);
    const cli = spawnSync(
      process.execPath,
      [
        path.join(__dirname, "b12-post-sign-readback.cjs"),
        "--layout-root",
        current.root,
        "--release-sha",
        RELEASE_SHA,
      ],
      { encoding: "utf8" },
    );
    assert.equal(cli.status, 0, cli.stderr);
    assert.equal(JSON.parse(cli.stdout).status, "PASS");
  } finally {
    rmSync(current.root, { recursive: true, force: true });
  }
});

test("reads a ZIP fixture once and catches post-sign size/hash mutation", () => {
  const current = fixture();
  const zipPath = path.join(current.root, "EchoDesk-unsigned-fixture.zip");
  try {
    const zip = spawnSync("zip", ["-q", "-r", zipPath, "Resources"], {
      cwd: current.root,
      encoding: "utf8",
    });
    assert.equal(zip.status, 0, zip.stderr);
    const zipResult = readback({ artifactPath: zipPath, expectedReleaseSha: RELEASE_SHA });
    assert.equal(zipResult.status, "PASS");
    assert.equal(zipResult.artifact.kind, "zip");

    writeFileSync(current.workerPath, "mutated after unsigned fixture freeze\n");
    const mutation = readback({ layoutRoot: current.root, expectedReleaseSha: RELEASE_SHA });
    assert.equal(mutation.status, "FAIL");
    assert.equal(mutation.verdict, "release_blocked_signing");
    assert.ok(mutation.failures.some((failure) => failure.code === "RESOURCE_SIZE_MISMATCH"));
    assert.ok(mutation.failures.some((failure) => failure.code === "RESOURCE_HASH_MISMATCH"));
  } finally {
    rmSync(current.root, { recursive: true, force: true });
  }
});

test("fails closed on partial size/hash and fallback scan status", () => {
  assert.throws(
    () => validateManifest({
      schema_version: 1,
      release_sha: RELEASE_SHA,
      content_entries: [{
        package_relative_path: "Resources/agent-runtime/worker.mjs",
        size_bytes: null,
        sha256: "a".repeat(64),
        role: "worker",
        platform: "darwin",
        arch: "arm64",
        executable: true,
        placement: "extraResources",
      }],
    }),
    /must set both size and sha256 when pending/,
  );

  const current = fixture();
  try {
    current.manifest.forbidden_fallback_scan = {
      status: "not_executed_by_B12_allowlist_manifest_worker",
      required_result: "pass_with_zero_external_runtime_fallbacks",
    };
    writeFileSync(
      path.join(current.runtimeRoot, "fusion-content-manifest.json"),
      JSON.stringify(current.manifest, null, 2),
    );
    const result = readback({ layoutRoot: current.root, expectedReleaseSha: RELEASE_SHA });
    assert.equal(result.status, "FAIL");
    assert.ok(result.failures.some((failure) => failure.code === "FORBIDDEN_FALLBACK_SCAN_NOT_PASS"));
  } finally {
    rmSync(current.root, { recursive: true, force: true });
  }
});

test("accepts the canonical manifest digest with manifest_digest.value omitted", () => {
  const current = fixture();
  try {
    current.manifest.manifest_digest.value = computeCanonicalManifestDigest(current.manifest);
    writeFileSync(
      path.join(current.runtimeRoot, "fusion-content-manifest.json"),
      JSON.stringify(current.manifest, null, 2),
    );
    const result = readback({ layoutRoot: current.root, expectedReleaseSha: RELEASE_SHA });
    assert.equal(result.status, "PASS");
    assert.equal(result.manifest_digest_status, "observed");
  } finally {
    rmSync(current.root, { recursive: true, force: true });
  }
});

test("recursively enumerates extension-independent PE files and detects portable byte drift", () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-b12-pe-scope-test-"));
  const innerRoot = path.join(root, "win-unpacked");
  const portableRoot = path.join(root, "portable");
  const outerPath = path.join(root, "EchoDesk.Setup.0.3.3.exe");
  const peBytes = (marker) => {
    const buffer = Buffer.alloc(96, 0);
    buffer[0] = 0x4d;
    buffer[1] = 0x5a;
    buffer.writeUInt32LE(64, 0x3c);
    buffer.write("PE\0\0", 64, "binary");
    buffer.write(marker, 68, "ascii");
    return buffer;
  };
  try {
    mkdirSync(path.join(innerRoot, "resources", "nested"), { recursive: true });
    mkdirSync(path.join(portableRoot, "resources", "nested"), { recursive: true });
    const files = [
      [path.join(innerRoot, "EchoDesk.exe"), "app"],
      [path.join(innerRoot, "resources", "backend.exe"), "backend"],
      [path.join(innerRoot, "resources", "nested", "helper.bin"), "helper"],
    ];
    for (const [filePath, marker] of files) writeFileSync(filePath, peBytes(marker));
    copyFileSync(files[0][0], path.join(portableRoot, "EchoDesk.exe"));
    copyFileSync(files[1][0], path.join(portableRoot, "resources", "backend.exe"));
    copyFileSync(files[2][0], path.join(portableRoot, "resources", "nested", "helper.bin"));
    writeFileSync(outerPath, peBytes("installer"));

    const scope = readbackWindowsPeScope({
      innerRoot,
      outerArtifacts: [outerPath],
      expectedReleaseSha: RELEASE_SHA,
    });
    assert.equal(scope.status, "PASS");
    assert.equal(scope.inner_pe_files.length, 3);
    assert.equal(scope.outer_pe_files.length, 1);
    assert.deepEqual(
      scope.inner_pe_files.map((entry) => entry.relative_path),
      ["EchoDesk.exe", "resources/backend.exe", "resources/nested/helper.bin"],
    );
    assert.equal(comparePeCoffTrees(innerRoot, portableRoot).status, "PASS");

    writeFileSync(path.join(portableRoot, "resources", "nested", "helper.bin"), peBytes("mutated"));
    const drift = comparePeCoffTrees(innerRoot, portableRoot);
    assert.equal(drift.status, "FAIL");
    assert.ok(drift.failures.some((failure) => failure.code === "PORTABLE_PE_BYTES_MISMATCH"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

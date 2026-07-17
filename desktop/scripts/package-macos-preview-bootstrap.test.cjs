const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const test = require("node:test");

const scriptDir = __dirname;
const packager = path.join(scriptDir, "package-macos-preview-bootstrap.sh");
const installer = path.join(
  scriptDir,
  "templates",
  "Install EchoDesk Preview.command",
);

test("installer policy is local, transactional, and contains no downloader", () => {
  const source = fs.readFileSync(installer, "utf8");

  assert.match(source, /xattr -r -d com\.apple\.quarantine "\$\{STAGED_BUNDLE\}"/);
  assert.match(source, /codesign --force --deep --sign - "\$\{STAGED_BUNDLE\}"/);
  assert.match(
    source,
    /codesign --verify --deep --strict --verbose=2 "\$\{TARGET_BUNDLE\}"/,
  );
  assert.match(source, /previous_moved=1/);
  assert.match(source, /mv -- "\$\{BACKUP_BUNDLE\}" "\$\{TARGET_BUNDLE\}"/);
  assert.doesNotMatch(
    source,
    /(^|[;&|]\s*)(curl|wget|spctl|defaults)\s|master-disable/m,
  );
});

test("packager emits a unique bound archive without tracked product bytes", () => {
  if (process.platform !== "darwin") {
    return;
  }

  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-preview-packager-test-"));
  const app = path.join(root, "Fixture.app");
  const output = path.join(root, "output");
  const contents = path.join(app, "Contents");
  fs.mkdirSync(contents, { recursive: true });
  fs.writeFileSync(path.join(contents, "fixture.txt"), "fixture-only\n");

  try {
    const stdout = execFileSync(
      packager,
      [
        "--app",
        app,
        "--release-sha",
        "0123456789abcdef0123456789abcdef01234567",
        "--version",
        "0.3.3-preview.3",
        "--output-dir",
        output,
      ],
      { encoding: "utf8" },
    );
    const values = Object.fromEntries(
      stdout
        .trim()
        .split("\n")
        .filter((line) => line.includes("="))
        .map((line) => line.split(/=(.*)/s).slice(0, 2)),
    );

    assert.ok(fs.existsSync(values.ZIP));
    assert.ok(fs.existsSync(values.MANIFEST));
    assert.ok(fs.existsSync(values.SHA256SUMS));
    assert.match(path.basename(values.ZIP), /0123456789ab-\d{8}T\d{6}Z-[A-Za-z0-9]+\.zip$/);

    const manifest = JSON.parse(fs.readFileSync(values.MANIFEST, "utf8"));
    assert.equal(manifest.release_sha, "0123456789abcdef0123456789abcdef01234567");
    assert.equal(manifest.version, "0.3.3-preview.3");
    assert.equal(manifest.bundle_path, "Payload/EchoDesk Preview.app");
    assert.match(manifest.payload_tree_sha256, /^[0-9a-f]{64}$/);

    execFileSync("/usr/bin/shasum", [
      "-a",
      "256",
      "-c",
      values.SHA256SUMS,
    ], { cwd: output });

    const listing = execFileSync("/usr/bin/ditto", ["-x", "-k", values.ZIP, path.join(root, "unpacked")]);
    assert.equal(listing.length, 0);
    const packageRoot = path.join(
      root,
      "unpacked",
      path.basename(values.ZIP, ".zip"),
    );
    assert.ok(fs.existsSync(path.join(packageRoot, "Payload", "EchoDesk Preview.app")));
    assert.ok(fs.existsSync(path.join(packageRoot, "Install EchoDesk Preview.command")));
    assert.deepEqual(
      JSON.parse(fs.readFileSync(path.join(packageRoot, "manifest.json"), "utf8")),
      manifest,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

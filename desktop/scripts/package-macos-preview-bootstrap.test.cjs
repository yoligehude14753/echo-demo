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
  assert.match(source, /shasum -a 256 --check --strict.*MANIFEST_SUMS/);
  assert.match(source, /plutil -extract release_sha raw/);
  assert.match(source, /Type INSTALL to continue/);
  assert.match(source, /codesign --force --deep --sign - "\$\{STAGED_BUNDLE\}"/);
  assert.match(
    source,
    /codesign --verify --deep --strict --verbose=2 "\$\{TARGET_BUNDLE\}"/,
  );
  assert.match(source, /previous_moved=1/);
  assert.match(source, /minimal fused workflow/);
  assert.match(source, /packaged fused worker bridge connected/);
  assert.match(source, /mv -- "\$\{BACKUP_BUNDLE\}" "\$\{TARGET_BUNDLE\}"/);
  assert.match(source, /rm -rf -- "\$\{BACKUP_BUNDLE\}"/);
  assert.doesNotMatch(
    source,
    /(^|[;&|]\s*)(curl|wget)\s*[^\n]*\|\s*(sh|bash)|spctl|defaults|master-disable/m,
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
  const resources = path.join(contents, "Resources");
  const executable = path.join(contents, "MacOS", "EchoDesk");
  fs.mkdirSync(path.join(resources, "backend"), { recursive: true });
  fs.mkdirSync(path.join(resources, "agent-runtime"), { recursive: true });
  fs.mkdirSync(path.dirname(executable), { recursive: true });
  fs.writeFileSync(
    path.join(contents, "Info.plist"),
    `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>EchoDesk</string>
  <key>CFBundleIdentifier</key><string>com.echodesk.fixture</string>
  <key>CFBundleName</key><string>EchoDesk</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
`,
  );
  fs.writeFileSync(executable, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  fs.writeFileSync(path.join(resources, "app.asar"), "fixture-only\n");
  fs.writeFileSync(path.join(resources, "backend", "echodesk-backend"), "fixture-only\n");
  fs.writeFileSync(path.join(resources, "agent-runtime", "worker.mjs"), "fixture-only\n");

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
    assert.ok(fs.existsSync(values.BOOTSTRAP));
    assert.ok(fs.existsSync(values.SHA256SUMS));
    assert.match(path.basename(values.ZIP), /0123456789ab-\d{8}T\d{6}Z-[A-Za-z0-9]+\.zip$/);

    const manifest = JSON.parse(fs.readFileSync(values.MANIFEST, "utf8"));
    assert.equal(manifest.release_sha, "0123456789abcdef0123456789abcdef01234567");
    assert.equal(manifest.version, "0.3.3-preview.3");
    assert.equal(manifest.bundle_path, "Payload/EchoDesk Preview.app");
    assert.equal(manifest.payload_checksums, "payload.sha256");
    assert.equal(manifest.manifest_checksum, "manifest.sha256");
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
    assert.ok(fs.existsSync(path.join(packageRoot, "manifest.sha256")));
    assert.ok(fs.existsSync(path.join(packageRoot, "payload.sha256")));
    assert.ok(fs.existsSync(path.join(packageRoot, "Install EchoDesk Preview.command")));
    execFileSync("/usr/bin/shasum", ["-a", "256", "--check", "--strict", "manifest.sha256"], { cwd: packageRoot });
    execFileSync("/usr/bin/shasum", ["-a", "256", "--check", "--strict", "payload.sha256"], { cwd: packageRoot });
    assert.deepEqual(
      JSON.parse(fs.readFileSync(path.join(packageRoot, "manifest.json"), "utf8")),
      manifest,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("bootstrap final-signs its copied payload before it creates the ZIP", () => {
  const source = fs.readFileSync(packager, "utf8");
  const signIndex = source.indexOf('mac-bundle-sign.cjs" "${PAYLOAD_BUNDLE}');
  const archiveIndex = source.indexOf('/usr/bin/ditto -c -k --sequesterRsrc --keepParent');

  assert.ok(signIndex >= 0, "bootstrap payload must use the final bundle signer");
  assert.ok(archiveIndex >= 0, "bootstrap must create its ZIP with ditto");
  assert.ok(signIndex < archiveIndex, "bootstrap must sign before it archives");
});

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { gunzipSync, gzipSync } = require("node:zlib");
const yaml = require("js-yaml");
const {
  buildBlockMap,
} = require("app-builder-lib/out/targets/blockmap/blockmap");

const {
  macReleaseContract,
  runMacRelease,
  runWindowsRelease,
  windowsReleaseContract,
} = require("../../scripts/desktop-release-signing.cjs");
const {
  refreshMacUpdateMetadata,
} = require("../../scripts/refresh-mac-update-metadata.cjs");
const {
  verifyReleaseUpdateMetadata,
} = require("../../scripts/verify-release-update-metadata.cjs");

const desktopRoot = path.resolve(__dirname, "../..");
const repoRoot = path.resolve(desktopRoot, "..");

const macIdentity =
  "Developer ID Application: EchoDesk Release (ABCDE12345)";
const validMacEnv = {
  CSC_NAME: macIdentity,
  APPLE_KEYCHAIN_PROFILE: "echodesk-notary",
};
const validWindowsEnv = {
  ECHODESK_WINDOWS_CERTIFICATE_SHA1: "ab".repeat(20),
  ECHODESK_WINDOWS_EXPECTED_PUBLISHER:
    "CN=EchoDesk Release, O=EchoDesk, C=US",
};
const silentLogger = { log() {} };

function ok(stdout = "", stderr = "") {
  return { status: 0, stdout, stderr };
}

function macRunner(calls, overrides = {}) {
  return (command, args, options) => {
    calls.push({ command, args: [...args], options });
    if (command === "security") {
      return (
        overrides.security ||
        ok(`  1) ${"A".repeat(40)} "${macIdentity}"\n     1 valid identities found`)
      );
    }
    if (command === "xcrun" && args[0] === "notarytool") {
      return overrides.notary || ok('{"id":"submission-123","status":"Accepted"}');
    }
    if (command === "codesign" && args[0] === "--display") {
      return (
        overrides.metadata ||
        ok(
          "",
          `Authority=${macIdentity}\nAuthority=Developer ID Certification Authority\nTeamIdentifier=ABCDE12345\n`,
        )
      );
    }
    return ok();
  };
}

test("formal macOS contract rejects missing, ad-hoc, and wrong identities", () => {
  assert.throws(
    () => macReleaseContract({ env: {}, platform: "darwin" }),
    /Missing required CSC_NAME/,
  );
  assert.throws(
    () =>
      macReleaseContract({
        env: { ...validMacEnv, ECHODESK_ADHOC_SIGN: "1" },
        platform: "darwin",
      }),
    /development-only/,
  );
  assert.throws(
    () =>
      macReleaseContract({
        env: { ...validMacEnv, CSC_NAME: "Apple Development: EchoDesk" },
        platform: "darwin",
      }),
    /exact Developer ID Application identity/,
  );
  assert.throws(
    () => macReleaseContract({ env: validMacEnv, platform: "linux" }),
    /must be built on macOS/,
  );
  assert.deepEqual(
    macReleaseContract({ env: validMacEnv, platform: "darwin" }),
    {
      identity: macIdentity,
      teamId: "ABCDE12345",
      keychainProfile: "echodesk-notary",
      keychain: "",
    },
  );
});

test("formal macOS build refuses a missing keychain identity before packaging", async () => {
  const calls = [];
  await assert.rejects(
    runMacRelease({
      env: validMacEnv,
      platform: "darwin",
      exists: () => true,
      logger: silentLogger,
      runner: macRunner(calls, {
        security: ok(
          `  1) ${"B".repeat(40)} "Developer ID Application: Somebody Else (ZZZZZ99999)"`,
        ),
      }),
    }),
    /Developer ID identity is not available/,
  );
  assert.equal(calls.some((call) => call.command === "npm"), false);
});

test("formal macOS build verifies Developer ID, notarization, Gatekeeper, and staples", async () => {
  const calls = [];
  const result = await runMacRelease({
    env: validMacEnv,
    platform: "darwin",
    exists: () => true,
    logger: silentLogger,
    runner: macRunner(calls),
  });

  assert.equal(result.notarizationId, "submission-123");
  assert.match(result.artifacts.dmgBlockmap, /\.dmg\.blockmap$/);
  assert.match(result.artifacts.zipBlockmap, /\.zip\.blockmap$/);
  assert.match(result.artifacts.updateMetadata, /latest-mac\.yml$/);
  const builder = calls.find(
    (call) => call.command === "npx" && call.args.includes("electron-builder"),
  );
  assert.ok(builder);
  assert.ok(builder.args.includes("--config.forceCodeSigning=true"));
  assert.ok(
    builder.args.includes(`--config.mac.identity=${macIdentity}`),
  );
  assert.ok(builder.args.includes("--config.mac.notarize=true"));
  assert.ok(builder.args.includes("--config.dmg.sign=true"));
  assert.ok(
    calls.some(
      (call) =>
        call.command === "xcrun" &&
        call.args[0] === "notarytool" &&
        call.args.includes("--wait"),
    ),
  );
  assert.ok(
    calls.some(
      (call) =>
        call.command === "xcrun" &&
        call.args[0] === "stapler" &&
        call.args[1] === "staple",
    ),
  );
  assert.equal(
    calls.filter(
      (call) =>
        call.command === "codesign" && call.args.includes("--strict"),
    ).length,
    2,
  );
  assert.equal(calls.filter((call) => call.command === "spctl").length, 2);
  assert.equal(
    calls.filter(
      (call) =>
        call.command === "xcrun" &&
        call.args[0] === "stapler" &&
        call.args[1] === "validate",
    ).length,
    2,
  );
  const stapleIndex = calls.findIndex(
    (call) =>
      call.command === "xcrun" &&
      call.args[0] === "stapler" &&
      call.args[1] === "staple",
  );
  const refreshIndex = calls.findIndex(
    (call) => call.args.some((arg) => /refresh-mac-update-metadata\.cjs$/.test(arg)),
  );
  const strictVerifyIndex = calls.findIndex(
    (call) => call.command === "codesign" && call.args.includes("--strict"),
  );
  assert.ok(stapleIndex >= 0 && refreshIndex > stapleIndex);
  assert.ok(strictVerifyIndex > refreshIndex);
});

test("final macOS updater metadata matches the post-staple artifact bytes", async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-mac-update-"));
  const releaseRoot = path.join(root, "release");
  const version = "0.3.1";
  const zipName = `EchoDesk-${version}-arm64-mac.zip`;
  const dmgName = `EchoDesk-${version}-arm64.dmg`;
  const zip = Buffer.from("final zip bytes");
  const dmg = Buffer.from("final dmg bytes after notarization ticket staple");
  try {
    mkdirSync(releaseRoot, { recursive: true });
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ version }),
    );
    writeFileSync(path.join(releaseRoot, zipName), zip);
    writeFileSync(path.join(releaseRoot, dmgName), dmg);
    writeFileSync(
      path.join(releaseRoot, "latest-mac.yml"),
      yaml.dump({
        version,
        files: [
          { url: zipName, sha512: "stale", size: 1 },
          { url: dmgName, sha512: "stale", size: 1 },
        ],
        path: zipName,
        sha512: "stale",
        releaseDate: "2026-07-12T00:00:00.000Z",
      }),
    );

    await refreshMacUpdateMetadata(root);

    const metadata = yaml.load(
      readFileSync(path.join(releaseRoot, "latest-mac.yml"), "utf8"),
    );
    const byUrl = new Map(metadata.files.map((entry) => [entry.url, entry]));
    const expectedZipHash = createHash("sha512").update(zip).digest("base64");
    const expectedDmgHash = createHash("sha512").update(dmg).digest("base64");
    assert.deepEqual(byUrl.get(zipName), {
      url: zipName,
      sha512: expectedZipHash,
      size: zip.length,
    });
    assert.deepEqual(byUrl.get(dmgName), {
      url: dmgName,
      sha512: expectedDmgHash,
      size: dmg.length,
    });
    assert.equal(metadata.sha512, expectedZipHash);
    for (const filename of [zipName, dmgName]) {
      const blockmap = path.join(releaseRoot, `${filename}.blockmap`);
      assert.equal(existsSync(blockmap), true);
      assert.ok(statSync(blockmap).size > 0);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("formal updater metadata binds artifacts and blockmaps to final bytes", async () => {
  const version = "0.3.1";
  for (const target of ["mac", "windows", "linux"]) {
    const root = mkdtempSync(
      path.join(os.tmpdir(), `echodesk-${target}-metadata-`),
    );
    const releaseRoot = path.join(root, "release");
    const filenames =
      target === "mac"
        ? [
            `EchoDesk-${version}-arm64-mac.zip`,
            `EchoDesk-${version}-arm64.dmg`,
          ]
        : target === "windows"
          ? [`EchoDesk.Setup.${version}.exe`]
          : [
              `EchoDesk-${version}-linux-x86_64.AppImage`,
              `EchoDesk-${version}-linux-amd64.deb`,
            ];
    const primary = filenames[0];
    const metadataName =
      target === "mac"
        ? "latest-mac.yml"
        : target === "windows"
          ? "latest.yml"
          : "latest-linux.yml";
    try {
      mkdirSync(releaseRoot, { recursive: true });
      writeFileSync(path.join(root, "package.json"), JSON.stringify({ version }));
      const entries = [];
      for (const [index, filename] of filenames.entries()) {
        const bytes = Buffer.alloc(32 + index, index + 1);
        const artifactPath = path.join(releaseRoot, filename);
        writeFileSync(artifactPath, bytes);
        let embeddedBlockmap = null;
        if (target === "linux" && index === 0) {
          embeddedBlockmap = await buildBlockMap(artifactPath, "deflate");
        } else if (target !== "linux") {
          await buildBlockMap(
            artifactPath,
            "gzip",
            path.join(releaseRoot, `${filename}.blockmap`),
          );
        }
        const finalBytes = readFileSync(artifactPath);
        const entry = {
          url: filename,
          size: finalBytes.length,
          sha512: createHash("sha512").update(finalBytes).digest("base64"),
        };
        if (embeddedBlockmap !== null) {
          entry.blockMapSize = embeddedBlockmap.blockMapSize;
        }
        entries.push(entry);
      }
      const metadataPath = path.join(releaseRoot, metadataName);
      const metadata = {
        version,
        files: entries,
        path: primary,
        sha512: entries[0].sha512,
      };
      writeFileSync(metadataPath, yaml.dump(metadata));

      if (target !== "linux") {
        for (const filename of filenames) {
          const blockmapPath = path.join(
            releaseRoot,
            `${filename}.blockmap`,
          );
          const original = readFileSync(blockmapPath);
          const alternate = gzipSync(gunzipSync(original), { level: 0 });
          assert.notDeepEqual(
            alternate,
            original,
            `${target} fixture must use a distinct valid gzip container`,
          );
          writeFileSync(blockmapPath, alternate);
        }
      }

      assert.equal(
        (await verifyReleaseUpdateMetadata(target, root)).version,
        version,
      );

      const invalidSize = structuredClone(metadata);
      invalidSize.files[0].size += 1;
      writeFileSync(metadataPath, yaml.dump(invalidSize));
      await assert.rejects(
        verifyReleaseUpdateMetadata(target, root),
        /size .* does not match/,
      );

      const invalidHash = structuredClone(metadata);
      invalidHash.files[0].sha512 = "invalid-sha512";
      invalidHash.sha512 = "invalid-sha512";
      writeFileSync(metadataPath, yaml.dump(invalidHash));
      await assert.rejects(
        verifyReleaseUpdateMetadata(target, root),
        /SHA-512 does not match final bytes/,
      );
      writeFileSync(metadataPath, yaml.dump(metadata));

      const primaryPath = path.join(releaseRoot, primary);
      if (target === "linux") {
        const corruptedBytes = Buffer.from(readFileSync(primaryPath));
        const blockMapSize = metadata.files[0].blockMapSize;
        corruptedBytes[corruptedBytes.length - blockMapSize - 4] ^= 0xff;
        writeFileSync(primaryPath, corruptedBytes);
        const corruptedMetadata = structuredClone(metadata);
        const corruptedHash = createHash("sha512")
          .update(corruptedBytes)
          .digest("base64");
        corruptedMetadata.files[0].sha512 = corruptedHash;
        corruptedMetadata.sha512 = corruptedHash;
        writeFileSync(metadataPath, yaml.dump(corruptedMetadata));
        await assert.rejects(
          verifyReleaseUpdateMetadata(target, root),
          /embedded blockmap does not match final artifact bytes/,
        );
      } else {
        const primaryBlockmap = `${primaryPath}.blockmap`;
        writeFileSync(primaryBlockmap, "corrupt blockmap");
        await assert.rejects(
          verifyReleaseUpdateMetadata(target, root),
          /not a valid bounded gzip blockmap/,
        );

        const oversizedRawBlockmap = Buffer.alloc(64 * 1024 * 1024 + 1);
        writeFileSync(
          primaryBlockmap,
          gzipSync(oversizedRawBlockmap, { level: 9 }),
        );
        await assert.rejects(
          verifyReleaseUpdateMetadata(target, root),
          /not a valid bounded gzip blockmap/,
        );

        await buildBlockMap(primaryPath, "gzip", primaryBlockmap);
        const validRawBlockmap = gunzipSync(readFileSync(primaryBlockmap));
        const checksumMarker = Buffer.from('"checksums":["');
        const markerIndex = validRawBlockmap.indexOf(checksumMarker);
        assert.ok(markerIndex >= 0);
        const checksumIndex = markerIndex + checksumMarker.length;
        validRawBlockmap[checksumIndex] =
          validRawBlockmap[checksumIndex] === 65 ? 66 : 65;
        writeFileSync(primaryBlockmap, gzipSync(validRawBlockmap));
        await assert.rejects(
          verifyReleaseUpdateMetadata(target, root),
          /blockmap does not match final artifact bytes/,
        );
      }
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }
});

test("formal macOS build refuses incomplete updater assets before notarization", async () => {
  const calls = [];
  await assert.rejects(
    runMacRelease({
      env: validMacEnv,
      platform: "darwin",
      exists: (artifactPath) => !artifactPath.endsWith("latest-mac.yml"),
      logger: silentLogger,
      runner: macRunner(calls),
    }),
    /Missing updateMetadata: .*latest-mac\.yml/,
  );
  assert.equal(
    calls.some(
      (call) =>
        call.command === "xcrun" && call.args[0] === "notarytool",
    ),
    false,
  );
});

test("formal macOS build rejects a non-Accepted notarization result", async () => {
  const calls = [];
  await assert.rejects(
    runMacRelease({
      env: validMacEnv,
      platform: "darwin",
      exists: () => true,
      logger: silentLogger,
      runner: macRunner(calls, {
        notary: ok('{"id":"submission-456","status":"Invalid"}'),
      }),
    }),
    /notarization was not accepted/,
  );
  assert.equal(
    calls.some(
      (call) =>
        call.command === "xcrun" &&
        call.args[0] === "stapler" &&
        call.args[1] === "staple",
    ),
    false,
  );
});

test("formal Windows contract rejects missing, malformed, and cross-platform inputs", () => {
  assert.throws(
    () => windowsReleaseContract({ env: {}, platform: "win32" }),
    /Missing required ECHODESK_WINDOWS_CERTIFICATE_SHA1/,
  );
  assert.throws(
    () =>
      windowsReleaseContract({
        env: {
          ECHODESK_WINDOWS_CERTIFICATE_SHA1: "ab".repeat(20),
        },
        platform: "win32",
      }),
    /Missing required ECHODESK_WINDOWS_EXPECTED_PUBLISHER/,
  );
  assert.throws(
    () =>
      windowsReleaseContract({
        env: {
          ...validWindowsEnv,
          ECHODESK_WINDOWS_CERTIFICATE_SHA1: "not-a-thumbprint",
        },
        platform: "win32",
      }),
    /40-character certificate thumbprint/,
  );
  assert.throws(
    () =>
      windowsReleaseContract({
        env: {
          ...validWindowsEnv,
          ECHODESK_WINDOWS_TIMESTAMP_URL: "https://user:secret@example.test",
        },
        platform: "win32",
      }),
    /without embedded credentials/,
  );
  assert.throws(
    () =>
      windowsReleaseContract({
        env: validWindowsEnv,
        platform: "darwin",
      }),
    /must be built on Windows/,
  );
  assert.deepEqual(
    windowsReleaseContract({ env: validWindowsEnv, platform: "win32" }),
    {
      certificateThumbprint: "AB".repeat(20),
      expectedPublisher: "CN=EchoDesk Release, O=EchoDesk, C=US",
      timestampUrl: "http://timestamp.digicert.com/",
    },
  );
});

test("formal Windows build refuses a wrong certificate before packaging", async () => {
  const calls = [];
  const runner = (command, args, options) => {
    calls.push({ command, args: [...args], options });
    if (command === "pwsh" && args.includes("Preflight")) {
      return { status: 1, stdout: "", stderr: "publisher mismatch" };
    }
    return ok();
  };
  await assert.rejects(
    runWindowsRelease({
      env: validWindowsEnv,
      platform: "win32",
      exists: () => true,
      logger: silentLogger,
      runner,
    }),
    /publisher mismatch/,
  );
  assert.equal(calls.some((call) => call.command === "npm.cmd"), false);
});

test("formal Windows build reports every missing signed candidate asset before verification", async () => {
  const version = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  ).version;
  const missingCases = [
    ["signed NSIS installer", `EchoDesk.Setup.${version}.exe`],
    ["signed unpacked application", path.join("win-unpacked", "EchoDesk.exe")],
    [
      "signed bundled backend",
      path.join(
        "win-unpacked",
        "resources",
        "backend",
        "echodesk-backend.exe",
      ),
    ],
    ["Windows portable ZIP", `EchoDesk-${version}-win-x64.zip`],
    ["NSIS installer blockmap", `EchoDesk.Setup.${version}.exe.blockmap`],
    ["Windows update metadata", "latest.yml"],
  ];

  for (const [label, missingSuffix] of missingCases) {
    const calls = [];
    await assert.rejects(
      runWindowsRelease({
        env: validWindowsEnv,
        platform: "win32",
        exists: (candidate) => !candidate.endsWith(missingSuffix),
        logger: silentLogger,
        runner: (command, args, options) => {
          calls.push({ command, args: [...args], options });
          return ok();
        },
      }),
      new RegExp(`Missing ${label}`),
    );
    assert.equal(
      calls.some(
        (call) => call.command === "pwsh" && call.args.includes("Verify"),
      ),
      false,
      `${label} must fail before Authenticode artifact verification`,
    );
  }
});

test("formal Windows build enforces Authenticode chain and timestamp verification", async () => {
  const calls = [];
  const runner = (command, args, options) => {
    calls.push({ command, args: [...args], options });
    return ok();
  };
  const result = await runWindowsRelease({
    env: validWindowsEnv,
    platform: "win32",
    exists: () => true,
    logger: silentLogger,
    runner,
  });

  assert.match(result.artifacts.installerBlockmap, /\.exe\.blockmap$/);
  assert.match(result.artifacts.updateMetadata, /latest\.yml$/);

  const builder = calls.find(
    (call) => call.command === "npx.cmd" && call.args.includes("electron-builder"),
  );
  assert.ok(builder);
  assert.ok(builder.args.includes("--config.win.forceCodeSigning=true"));
  assert.ok(
    builder.args.includes(
      `--config.win.signtoolOptions.certificateSha1=${"AB".repeat(20)}`,
    ),
  );
  assert.ok(
    builder.args.includes(
      "--config.win.signtoolOptions.signingHashAlgorithms=sha256",
    ),
  );
  assert.ok(
    builder.args.some((arg) =>
      arg.startsWith(
        "--config.win.signtoolOptions.rfc3161TimeStampServer=http://timestamp.digicert.com/",
      ),
    ),
  );
  assert.equal(
    calls.filter(
      (call) => call.command === "pwsh" && call.args.includes("Verify"),
    ).length,
    3,
  );
});

test("PowerShell verifier and CI encode an honest signing contract", () => {
  const verifier = readFileSync(
    path.join(desktopRoot, "scripts/verify-windows-authenticode.ps1"),
    "utf8",
  );
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const ci = readFileSync(
    path.join(repoRoot, ".github/workflows/ci.yml"),
    "utf8",
  );
  const windows = readFileSync(
    path.join(repoRoot, ".github/workflows/build-windows-installer.yml"),
    "utf8",
  );
  const formalDesktop = readFileSync(
    path.join(
      repoRoot,
      ".github/workflows/build-desktop-release-candidates.yml",
    ),
    "utf8",
  );

  assert.match(verifier, /Get-AuthenticodeSignature/);
  assert.match(verifier, /SignatureStatus\]::Valid/);
  assert.match(verifier, /X509Chain/);
  assert.match(verifier, /X509RevocationMode\]::Online/);
  assert.match(verifier, /TimeStamperCertificate/);
  assert.match(verifier, /OrdinalIgnoreCase\.Equals/);
  assert.equal(
    pkg.scripts["app:dist:mac"],
    "node scripts/desktop-release-signing.cjs mac",
  );
  assert.equal(
    pkg.scripts["app:dist:win"],
    "node scripts/desktop-release-signing.cjs windows",
  );
  assert.match(pkg.scripts["app:dist:mac:adhoc-test"], /ECHODESK_ADHOC_SIGN=1/);
  assert.match(
    pkg.scripts["app:dist:win:unsigned-test"],
    /CSC_IDENTITY_AUTO_DISCOVERY=false/,
  );
  assert.match(ci, /npm run app:dist:mac:adhoc-test/);
  assert.doesNotMatch(ci, /npm run app:dist:mac(?:\s|$)/m);
  assert.match(windows, /npm run app:dist:win:unsigned-test/);
  assert.doesNotMatch(windows, /npm run app:dist:win(?:\s|$)/m);
  const unsignedWorkflow = yaml.load(windows);
  const unsignedUploads = unsignedWorkflow.jobs["build-windows"].steps.filter(
    (step) => String(step.uses || "").startsWith("actions/upload-artifact@"),
  );
  assert.ok(
    unsignedUploads.length >= 1,
    "unsigned smoke evidence must be retained",
  );
  for (const upload of unsignedUploads) {
    assert.doesNotMatch(
      String(upload.with?.path || ""),
      /desktop\/release|EchoDesk\.Setup|win-x64\.zip|latest\.yml|SBOM|SHA256SUMS/,
      "unsigned release assets must never be uploaded",
    );
  }
  assert.doesNotMatch(windows, /name: echodesk-windows-unsigned-test/);
  assert.doesNotMatch(windows, /gh release upload/);

  const formalWorkflow = yaml.load(formalDesktop);
  const authorizeMain = formalWorkflow.jobs["authorize-main"];
  assert.equal(authorizeMain.permissions.actions, "read");
  const authorizationSteps = authorizeMain.steps;
  const environmentGuardIndex = authorizationSteps.findIndex(
    (step) =>
      step.name === "Require pre-existing protected Windows release environment",
  );
  const exactShaCiIndex = authorizationSteps.findIndex((step) =>
    String(step.run || "").includes("/actions/workflows/ci.yml/runs?"),
  );
  assert.ok(
    environmentGuardIndex >= 0 && environmentGuardIndex < exactShaCiIndex,
    "protected Windows environment must be verified before exact-SHA CI authorization",
  );
  const environmentGuard = authorizationSteps[environmentGuardIndex];
  assert.equal(environmentGuard.env.GH_TOKEN, "${{ github.token }}");
  assert.match(environmentGuard.run, /^set -euo pipefail$/m);
  assert.match(
    environmentGuard.run,
    /environments\/desktop-release-windows/,
  );
  assert.match(environmentGuard.run, /\.can_admins_bypass == false/);
  assert.match(environmentGuard.run, /required_reviewers/);
  assert.match(environmentGuard.run, /custom_branch_policies/);
  assert.match(environmentGuard.run, /deployment-branch-policies/);
  assert.match(environmentGuard.run, /\.name == "main"/);
  assert.doesNotMatch(environmentGuard.run, /\|\| true|2>\/?dev\/null/);

  const formalWindows = formalWorkflow.jobs["windows-signed-candidate"];
  assert.equal(formalWindows.environment, "desktop-release-windows");
  const formalSteps = formalWindows.steps;
  const credentialIndex = formalSteps.findIndex(
    (step) => step.name === "Require protected Windows release credentials",
  );
  const setupIndex = formalSteps.findIndex((step) =>
    /actions\/setup-(?:python|node)@/.test(String(step.uses || "")),
  );
  const installIndex = formalSteps.findIndex(
    (step) => step.name === "Install locked build dependencies",
  );
  const buildIndex = formalSteps.findIndex(
    (step) => step.name === "Build, sign, timestamp, and verify Windows candidate",
  );
  assert.ok(
    credentialIndex >= 0,
    "Windows release credential preflight is required",
  );
  assert.ok(
    [setupIndex, installIndex, buildIndex].every(
      (index) => index > credentialIndex,
    ),
    "Windows credential preflight must run before setup, install, and build",
  );
  const credentialStep = formalSteps[credentialIndex];
  for (const secret of [
    "ECHODESK_WINDOWS_CERTIFICATE_PFX_BASE64",
    "ECHODESK_WINDOWS_CERTIFICATE_PASSWORD",
    "ECHODESK_WINDOWS_CERTIFICATE_SHA1",
    "ECHODESK_WINDOWS_EXPECTED_PUBLISHER",
  ]) {
    assert.equal(credentialStep.env[secret], `\${{ secrets.${secret} }}`);
    assert.match(
      credentialStep.run,
      new RegExp(`(?:^|\\s)${secret}(?:\\s|$)`),
      `${secret} must be named in the fail-fast missing-secret report`,
    );
  }
  const attestation = formalSteps.find((step) =>
    String(step.uses || "").startsWith("actions/attest-build-provenance@"),
  );
  const signedUpload = formalSteps.find(
    (step) => step.name === "Upload signed Windows candidate without publishing",
  );
  assert.ok(attestation, "signed Windows candidate must have provenance");
  assert.ok(signedUpload, "signed Windows candidate must be retained for review");
  const expectedSignedAssets = [
    "desktop/release/EchoDesk-${{ env.ECHODESK_VERSION }}-win-x64.zip",
    "desktop/release/EchoDesk.Setup.${{ env.ECHODESK_VERSION }}.exe",
    "desktop/release/EchoDesk.Setup.${{ env.ECHODESK_VERSION }}.exe.blockmap",
    "desktop/release/EchoDesk-SBOM.cdx.json",
    "desktop/release/SHA256SUMS-Windows.txt",
    "desktop/release/latest.yml",
  ].sort();
  const assetLines = (value) =>
    String(value || "")
      .trim()
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .sort();
  assert.deepEqual(
    assetLines(attestation.with["subject-path"]),
    expectedSignedAssets,
  );
  assert.deepEqual(assetLines(signedUpload.with.path), expectedSignedAssets);
});

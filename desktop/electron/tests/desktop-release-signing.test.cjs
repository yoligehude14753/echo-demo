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
  resolveMacIdentity,
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
const desktopPackage = JSON.parse(readFileSync(path.join(desktopRoot, "package.json"), "utf8"));

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
    if ((command === "codesign" || command === "/usr/bin/codesign") && args[0] === "--display") {
      if (args.includes("--entitlements")) {
        return ok("<plist><dict><key>com.apple.security.device.audio-input</key><true/><key>com.apple.security.cs.allow-jit</key><true/><key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/></dict></plist>");
      }
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

test("formal macOS contract does not accept identity text from the environment", () => {
  assert.deepEqual(macReleaseContract({ env: {}, platform: "darwin" }), {
    keychainProfile: "",
    keychain: "",
  });
  assert.throws(
    () =>
      macReleaseContract({
        env: { ...validMacEnv, ECHODESK_ADHOC_SIGN: "1" },
        platform: "darwin",
      }),
    /development-only/,
  );
  assert.throws(
    () => macReleaseContract({ env: validMacEnv, platform: "linux" }),
    /must be built on macOS/,
  );
  assert.deepEqual(
    macReleaseContract({ env: validMacEnv, platform: "darwin" }),
    {
      keychainProfile: "echodesk-notary",
      keychain: "",
    },
  );
});
test("identity resolver rejects zero matches and selects one installed-Team certificate", () => {
  assert.throws(() => resolveMacIdentity("0 valid identities found", "ABCDE12345"), /no valid/);
  const one = resolveMacIdentity(`1) ${"A".repeat(40)} "${macIdentity}"`, "ABCDE12345");
  assert.equal(one.teamId, "ABCDE12345");
});

test("identity resolver deterministically selects the newest valid duplicate", () => {
  const older = "A".repeat(40);
  const newer = "B".repeat(40);
  const output = `1) ${older} "${macIdentity}"\n2) ${newer} "${macIdentity}"`;
  const selected = resolveMacIdentity(output, "ABCDE12345", {
    [older]: "2025-01-01T00:00:00Z",
    [newer]: "2026-01-01T00:00:00Z",
  });
  assert.equal(selected.hash, newer);
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
    /no valid installed-Team Developer ID identity/,
  );
  assert.equal(calls.some((call) => call.command === "npm"), false);
});

test("formal macOS build produces and verifies one Developer ID signed app bundle", async () => {
  const calls = [];
  const signingHashes = [];
  const result = await runMacRelease({
    env: validMacEnv,
    platform: "darwin",
    exists: () => true,
    logger: silentLogger,
    runner: macRunner(calls),
    signBundle(_appPath, signingHash) { signingHashes.push(signingHash); },
  });

  assert.match(result.artifacts.app, /release\/mac-arm64\/EchoDesk\.app$/);
  assert.deepEqual(signingHashes, ["A".repeat(40)]);
  const builder = calls.find(
    (call) => call.command === "npx" && call.args.includes("electron-builder"),
  );
  assert.ok(builder);
  assert.ok(builder.args.includes("--config.forceCodeSigning=false"));
  assert.ok(builder.args.includes("--config.mac.identity=null"));
  assert.ok(builder.args.includes("--config.mac.notarize=false"));
  assert.ok(builder.args.includes("--config.dmg.sign=false"));
  assert.ok(builder.args.includes("--config.electronDist=node_modules/electron/dist"));
  assert.ok(builder.args.includes("dir"));
  assert.equal(builder.args.includes("dmg"), false);
  assert.equal(builder.args.includes("zip"), false);
  assert.equal(
    calls.filter(
      (call) =>
        call.command === "codesign" && call.args.includes("--strict"),
    ).length,
    1,
  );
  assert.equal(calls.some((call) => call.command === "xcrun" || call.command === "spctl"), false);
});

test("formal macOS app-only build uses only the repository locked Electron distribution", () => {
  assert.equal(desktopPackage.build.electronDist, "node_modules/electron/dist");
  assert.equal(desktopPackage.build.mac.target[0], "dir");
});

test("formal macOS build remains Developer-ID signed without a notary profile", async () => {
  const calls = [];
  const result = await runMacRelease({
    env: {},
    platform: "darwin",
    exists: () => true,
    logger: silentLogger,
    runner: macRunner(calls),
    signBundle() {},
  });
  const builder = calls.find((call) => call.command === "npx");
  assert.ok(builder.args.includes("--config.mac.notarize=false"));
  assert.equal(calls.some((call) => call.command === "xcrun" || call.command === "spctl"), false);
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

test("formal macOS build refuses a missing app bundle before signing", async () => {
  const calls = [];
  await assert.rejects(
    runMacRelease({
      env: validMacEnv,
      platform: "darwin",
      exists: (artifactPath) => !artifactPath.endsWith("EchoDesk.app"),
      logger: silentLogger,
      runner: macRunner(calls),
    }),
    /Missing app: .*EchoDesk\.app/,
  );
  assert.equal(
    calls.some(
      (call) =>
        call.command === "xcrun" && call.args[0] === "notarytool",
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

test("current release entrypoints stay explicit while retired CI workflows remain absent", () => {
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
  const windowsArtifact = readFileSync(
    path.join(repoRoot, ".github/workflows/windows-desktop-artifact.yml"),
    "utf8",
  );

  for (const relativePath of [
    ".github/workflows/build-windows-installer.yml",
    ".github/workflows/build-desktop-release-candidates.yml",
  ]) {
    assert.equal(existsSync(path.join(repoRoot, relativePath)), false, relativePath);
  }

  assert.match(verifier, /Get-AuthenticodeSignature/);
  assert.match(verifier, /SignatureStatus\]::Valid/);
  assert.match(verifier, /X509Chain/);
  assert.match(verifier, /X509RevocationMode\]::Online/);
  assert.match(verifier, /TimeStamperCertificate/);
  assert.equal(pkg.scripts["app:dist"], "npm run release:canonical:mac");
  assert.equal(
    pkg.scripts["app:dist:mac"],
    "node scripts/desktop-release-signing.cjs mac",
  );
  assert.equal(pkg.scripts["app:dist:mac:adhoc-test"], undefined);
  assert.match(
    pkg.scripts["app:dist:win:unsigned-test"],
    /CSC_IDENTITY_AUTO_DISCOVERY=false/,
  );
  assert.doesNotMatch(ci, /build-windows-installer|build-desktop-release-candidates/);
  assert.match(windowsArtifact, /npm run app:dist:win:unsigned-test/);
  assert.doesNotMatch(windowsArtifact, /npm run app:dist:win(?:\s|$)/m);
});

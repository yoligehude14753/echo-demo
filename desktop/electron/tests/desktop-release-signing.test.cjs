const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  macReleaseContract,
  runMacRelease,
  runWindowsRelease,
  windowsReleaseContract,
} = require("../../scripts/desktop-release-signing.cjs");

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
    /40-character certificate thumbprint/,
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

test("formal Windows build enforces Authenticode chain and timestamp verification", async () => {
  const calls = [];
  const runner = (command, args, options) => {
    calls.push({ command, args: [...args], options });
    return ok();
  };
  await runWindowsRelease({
    env: validWindowsEnv,
    platform: "win32",
    exists: () => true,
    logger: silentLogger,
    runner,
  });

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
  assert.match(windows, /name: echodesk-windows-unsigned-test/);
  assert.doesNotMatch(windows, /gh release upload/);
});

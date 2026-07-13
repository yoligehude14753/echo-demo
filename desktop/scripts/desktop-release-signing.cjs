/* eslint-disable no-console */
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const WINDOWS_TIMESTAMP_URL = "http://timestamp.digicert.com";

function requiredText(env, name) {
  const value = String(env[name] || "").trim();
  if (!value) {
    throw new Error(`[release-signing] Missing required ${name}`);
  }
  if (/[\0\r\n]/.test(value)) {
    throw new Error(`[release-signing] Invalid ${name}: control characters are forbidden`);
  }
  return value;
}

function normalizeCertificateThumbprint(value) {
  const normalized = String(value || "")
    .replace(/[\s:]/g, "")
    .toUpperCase();
  if (!/^[0-9A-F]{40}$/.test(normalized)) {
    throw new Error(
      "[release-signing] ECHODESK_WINDOWS_CERTIFICATE_SHA1 must be a 40-character certificate thumbprint",
    );
  }
  return normalized;
}

function macReleaseContract({
  env = process.env,
  platform = process.platform,
} = {}) {
  if (platform !== "darwin") {
    throw new Error("[release-signing] Formal macOS releases must be built on macOS");
  }
  if (env.ECHODESK_ADHOC_SIGN === "1") {
    throw new Error(
      "[release-signing] ECHODESK_ADHOC_SIGN=1 is development-only and cannot be used for a formal release",
    );
  }
  if (String(env.CSC_IDENTITY_AUTO_DISCOVERY || "").toLowerCase() === "false") {
    throw new Error(
      "[release-signing] CSC_IDENTITY_AUTO_DISCOVERY=false disables formal Developer ID signing",
    );
  }

  const identity = requiredText(env, "CSC_NAME");
  const identityMatch = /^Developer ID Application: .+ \(([A-Z0-9]{10})\)$/.exec(
    identity,
  );
  if (!identityMatch) {
    throw new Error(
      "[release-signing] CSC_NAME must be an exact Developer ID Application identity ending in a 10-character Team ID",
    );
  }

  const keychainProfile = requiredText(env, "APPLE_KEYCHAIN_PROFILE");
  const keychain = String(env.APPLE_KEYCHAIN || "").trim();
  if (/[\0\r\n]/.test(keychain)) {
    throw new Error("[release-signing] Invalid APPLE_KEYCHAIN");
  }

  return {
    identity,
    teamId: identityMatch[1],
    keychainProfile,
    keychain,
  };
}

function windowsReleaseContract({
  env = process.env,
  platform = process.platform,
} = {}) {
  if (platform !== "win32") {
    throw new Error("[release-signing] Formal Windows releases must be built on Windows");
  }

  const certificateThumbprint = normalizeCertificateThumbprint(
    requiredText(env, "ECHODESK_WINDOWS_CERTIFICATE_SHA1"),
  );
  const expectedPublisher = requiredText(
    env,
    "ECHODESK_WINDOWS_EXPECTED_PUBLISHER",
  );
  const timestampUrl = String(
    env.ECHODESK_WINDOWS_TIMESTAMP_URL || WINDOWS_TIMESTAMP_URL,
  ).trim();
  let parsedTimestampUrl;
  try {
    parsedTimestampUrl = new URL(timestampUrl);
  } catch {
    throw new Error("[release-signing] Invalid ECHODESK_WINDOWS_TIMESTAMP_URL");
  }
  if (
    !["http:", "https:"].includes(parsedTimestampUrl.protocol) ||
    parsedTimestampUrl.username ||
    parsedTimestampUrl.password
  ) {
    throw new Error(
      "[release-signing] ECHODESK_WINDOWS_TIMESTAMP_URL must be an HTTP(S) URL without embedded credentials",
    );
  }

  return {
    certificateThumbprint,
    expectedPublisher,
    timestampUrl: parsedTimestampUrl.toString(),
  };
}

function defaultRunner(command, args, options = {}) {
  const capture = options.capture === true;
  const result = spawnSync(command, args, {
    cwd: options.cwd || DESKTOP_ROOT,
    env: options.env || process.env,
    encoding: "utf8",
    stdio: capture ? "pipe" : "inherit",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(
      `[release-signing] ${options.label || command} could not start: ${result.error.message}`,
    );
  }
  return {
    status: result.status,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
  };
}

function runChecked(runner, command, args, options = {}) {
  const label = options.label || command;
  const result = runner(command, args, options) || {};
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr]
      .filter(Boolean)
      .join("\n")
      .trim();
    throw new Error(
      `[release-signing] ${label} failed${detail ? `: ${detail}` : ""}`,
    );
  }
  return {
    stdout: String(result.stdout || ""),
    stderr: String(result.stderr || ""),
  };
}

function assertMacIdentityAvailable(output, expectedIdentity) {
  const identities = [];
  for (const match of String(output).matchAll(
    /^\s*\d+\)\s+[0-9A-Fa-f]{40}\s+"([^"]+)"\s*$/gm,
  )) {
    identities.push(match[1]);
  }
  if (!identities.includes(expectedIdentity)) {
    throw new Error(
      `[release-signing] Developer ID identity is not available in the login keychain: ${expectedIdentity}`,
    );
  }
}

function assertMacSignatureMetadata(output, contract, artifactLabel) {
  const authorities = [];
  let teamIdentifier = "";
  for (const line of String(output).split(/\r?\n/)) {
    if (line.startsWith("Authority=")) {
      authorities.push(line.slice("Authority=".length).trim());
    } else if (line.startsWith("TeamIdentifier=")) {
      teamIdentifier = line.slice("TeamIdentifier=".length).trim();
    }
  }
  if (authorities[0] !== contract.identity) {
    throw new Error(
      `[release-signing] ${artifactLabel} was not signed by the required Developer ID identity`,
    );
  }
  if (teamIdentifier !== contract.teamId) {
    throw new Error(
      `[release-signing] ${artifactLabel} TeamIdentifier does not match ${contract.teamId}`,
    );
  }
}

function assertNotaryAccepted(output) {
  let result;
  try {
    result = JSON.parse(String(output).trim());
  } catch {
    throw new Error("[release-signing] notarytool did not return valid JSON");
  }
  if (result.status !== "Accepted" || typeof result.id !== "string" || !result.id) {
    throw new Error(
      `[release-signing] Apple notarization was not accepted (status=${String(result.status || "unknown")})`,
    );
  }
  return result.id;
}

function ensureArtifactsExist(artifacts, exists = existsSync) {
  for (const [label, artifactPath] of Object.entries(artifacts)) {
    if (!exists(artifactPath)) {
      throw new Error(`[release-signing] Missing ${label}: ${artifactPath}`);
    }
  }
}

function packageVersion(desktopRoot = DESKTOP_ROOT) {
  const pkg = JSON.parse(readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  return pkg.version;
}

async function runMacRelease({
  env = process.env,
  platform = process.platform,
  runner = defaultRunner,
  exists = existsSync,
  desktopRoot = DESKTOP_ROOT,
  logger = console,
} = {}) {
  const contract = macReleaseContract({ env, platform });
  const releaseEnv = {
    ...env,
    ECHODESK_ADHOC_SIGN: "0",
    CSC_NAME: contract.identity,
    APPLE_KEYCHAIN_PROFILE: contract.keychainProfile,
  };
  const commandOptions = { cwd: desktopRoot, env: releaseEnv };

  const identityResult = runChecked(
    runner,
    "security",
    ["find-identity", "-v", "-p", "codesigning"],
    {
      ...commandOptions,
      capture: true,
      label: "Developer ID identity preflight",
    },
  );
  assertMacIdentityAvailable(
    `${identityResult.stdout}\n${identityResult.stderr}`,
    contract.identity,
  );

  runChecked(runner, "npm", ["run", "backend:build:mac"], {
    ...commandOptions,
    label: "macOS backend build",
  });
  runChecked(runner, "npm", ["run", "build"], {
    ...commandOptions,
    label: "desktop renderer build",
  });
  runChecked(
    runner,
    "npx",
    [
      "--no-install",
      "electron-builder",
      "--mac",
      "dmg",
      "zip",
      "--arm64",
      "--publish",
      "never",
      "--config.forceCodeSigning=true",
      `--config.mac.identity=${contract.identity}`,
      "--config.mac.notarize=true",
      "--config.dmg.sign=true",
    ],
    { ...commandOptions, label: "Developer ID macOS package build" },
  );

  const version = packageVersion(desktopRoot);
  const artifacts = {
    app: path.join(desktopRoot, "release", "mac-arm64", "EchoDesk.app"),
    dmg: path.join(desktopRoot, "release", `EchoDesk-${version}-arm64.dmg`),
    dmgBlockmap: path.join(
      desktopRoot,
      "release",
      `EchoDesk-${version}-arm64.dmg.blockmap`,
    ),
    zip: path.join(
      desktopRoot,
      "release",
      `EchoDesk-${version}-arm64-mac.zip`,
    ),
    zipBlockmap: path.join(
      desktopRoot,
      "release",
      `EchoDesk-${version}-arm64-mac.zip.blockmap`,
    ),
    updateMetadata: path.join(desktopRoot, "release", "latest-mac.yml"),
  };
  ensureArtifactsExist(artifacts, exists);

  const notaryArgs = [
    "notarytool",
    "submit",
    artifacts.dmg,
    "--keychain-profile",
    contract.keychainProfile,
  ];
  if (contract.keychain) {
    notaryArgs.push("--keychain", contract.keychain);
  }
  notaryArgs.push("--wait", "--output-format", "json");
  const notaryResult = runChecked(runner, "xcrun", notaryArgs, {
    ...commandOptions,
    capture: true,
    label: "DMG notarization",
  });
  const notarizationId = assertNotaryAccepted(notaryResult.stdout);

  runChecked(runner, "xcrun", ["stapler", "staple", artifacts.dmg], {
    ...commandOptions,
    label: "DMG notarization ticket staple",
  });
  runChecked(
    runner,
    process.execPath,
    [path.join(desktopRoot, "scripts", "refresh-mac-update-metadata.cjs")],
    {
      ...commandOptions,
      label: "final macOS updater metadata refresh",
    },
  );

  for (const [label, artifactPath] of [
    ["application", artifacts.app],
    ["DMG", artifacts.dmg],
  ]) {
    runChecked(
      runner,
      "codesign",
      ["--verify", "--deep", "--strict", "--verbose=2", artifactPath],
      {
        ...commandOptions,
        capture: true,
        label: `${label} strict codesign verification`,
      },
    );
    const metadata = runChecked(
      runner,
      "codesign",
      ["--display", "--verbose=4", artifactPath],
      {
        ...commandOptions,
        capture: true,
        label: `${label} codesign metadata verification`,
      },
    );
    assertMacSignatureMetadata(
      `${metadata.stdout}\n${metadata.stderr}`,
      contract,
      label,
    );
    runChecked(runner, "xcrun", ["stapler", "validate", artifactPath], {
      ...commandOptions,
      capture: true,
      label: `${label} notarization ticket validation`,
    });
  }

  runChecked(
    runner,
    "spctl",
    ["--assess", "--type", "execute", "--verbose=4", artifacts.app],
    {
      ...commandOptions,
      capture: true,
      label: "application Gatekeeper assessment",
    },
  );
  runChecked(
    runner,
    "spctl",
    [
      "--assess",
      "--type",
      "open",
      "--context",
      "context:primary-signature",
      "--verbose=4",
      artifacts.dmg,
    ],
    {
      ...commandOptions,
      capture: true,
      label: "DMG Gatekeeper assessment",
    },
  );

  logger.log(
    `[release-signing] Verified Developer ID, notarization ${notarizationId}, Gatekeeper, and stapled tickets`,
  );
  return { contract, artifacts, notarizationId };
}

async function runWindowsRelease({
  env = process.env,
  platform = process.platform,
  runner = defaultRunner,
  exists = existsSync,
  desktopRoot = DESKTOP_ROOT,
  logger = console,
} = {}) {
  const contract = windowsReleaseContract({ env, platform });
  const commandOptions = { cwd: desktopRoot, env };
  const verifier = path.join(
    desktopRoot,
    "scripts",
    "verify-windows-authenticode.ps1",
  );
  ensureArtifactsExist({ "Authenticode verifier": verifier }, exists);

  const verifierArgs = [
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    verifier,
  ];
  const contractArgs = [
    "-Thumbprint",
    contract.certificateThumbprint,
    "-ExpectedPublisher",
    contract.expectedPublisher,
  ];
  runChecked(
    runner,
    "pwsh",
    [...verifierArgs, "-Mode", "Preflight", ...contractArgs],
    {
      ...commandOptions,
      capture: true,
      label: "Windows signing certificate preflight",
    },
  );

  runChecked(runner, "npm.cmd", ["run", "backend:build:win"], {
    ...commandOptions,
    label: "Windows backend build",
  });
  runChecked(runner, "npm.cmd", ["run", "build"], {
    ...commandOptions,
    label: "desktop renderer build",
  });
  runChecked(
    runner,
    "npx.cmd",
    [
      "--no-install",
      "electron-builder",
      "--win",
      "nsis",
      "zip",
      "--x64",
      "--publish",
      "never",
      "--config.win.forceCodeSigning=true",
      `--config.win.signtoolOptions.certificateSha1=${contract.certificateThumbprint}`,
      "--config.win.signtoolOptions.signingHashAlgorithms=sha256",
      `--config.win.signtoolOptions.rfc3161TimeStampServer=${contract.timestampUrl}`,
    ],
    { ...commandOptions, label: "Authenticode Windows package build" },
  );

  const version = packageVersion(desktopRoot);
  const artifacts = {
    installer: path.join(
      desktopRoot,
      "release",
      `EchoDesk.Setup.${version}.exe`,
    ),
    application: path.join(
      desktopRoot,
      "release",
      "win-unpacked",
      "EchoDesk.exe",
    ),
    backend: path.join(
      desktopRoot,
      "release",
      "win-unpacked",
      "resources",
      "backend",
      "echodesk-backend.exe",
    ),
    zip: path.join(
      desktopRoot,
      "release",
      `EchoDesk-${version}-win-x64.zip`,
    ),
    installerBlockmap: path.join(
      desktopRoot,
      "release",
      `EchoDesk.Setup.${version}.exe.blockmap`,
    ),
    updateMetadata: path.join(desktopRoot, "release", "latest.yml"),
  };
  ensureArtifactsExist(
    {
      "signed NSIS installer": artifacts.installer,
      "signed unpacked application": artifacts.application,
      "signed bundled backend": artifacts.backend,
      "Windows portable ZIP": artifacts.zip,
      "NSIS installer blockmap": artifacts.installerBlockmap,
      "Windows update metadata": artifacts.updateMetadata,
    },
    exists,
  );

  for (const [label, artifactPath] of Object.entries(artifacts)) {
    if (!artifactPath.toLowerCase().endsWith(".exe")) continue;
    runChecked(
      runner,
      "pwsh",
      [
        ...verifierArgs,
        "-Mode",
        "Verify",
        ...contractArgs,
        "-ArtifactPath",
        artifactPath,
      ],
      {
        ...commandOptions,
        capture: true,
        label: `${label} Authenticode chain and timestamp verification`,
      },
    );
  }

  logger.log(
    "[release-signing] Verified Authenticode publisher, certificate chains, and RFC 3161 timestamps",
  );
  return { contract, artifacts };
}

async function main(argv = process.argv.slice(2)) {
  const target = argv[0];
  if (target === "mac") {
    await runMacRelease();
    return;
  }
  if (target === "windows") {
    await runWindowsRelease();
    return;
  }
  throw new Error(
    "Usage: node scripts/desktop-release-signing.cjs <mac|windows>",
  );
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}

module.exports = {
  assertMacIdentityAvailable,
  assertMacSignatureMetadata,
  assertNotaryAccepted,
  macReleaseContract,
  normalizeCertificateThumbprint,
  runMacRelease,
  runWindowsRelease,
  windowsReleaseContract,
};

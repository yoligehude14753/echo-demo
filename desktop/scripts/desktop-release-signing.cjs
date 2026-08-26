/* eslint-disable no-console */
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { enumeratePeCoffFiles } = require("./pe-coff.cjs");
const { signDeveloperIdMacBundle } = require("./mac-bundle-sign.cjs");

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

  const keychainProfile = String(env.APPLE_KEYCHAIN_PROFILE || "").trim();
  if (/[^\x20-\x7E]/.test(keychainProfile)) {
    throw new Error("[release-signing] Invalid APPLE_KEYCHAIN_PROFILE");
  }
  const keychain = String(env.APPLE_KEYCHAIN || "").trim();
  if (/[\0\r\n]/.test(keychain)) {
    throw new Error("[release-signing] Invalid APPLE_KEYCHAIN");
  }

  return {
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

function installedMacTeamId(output) {
  const match = /^TeamIdentifier=([A-Z0-9]{10})$/m.exec(String(output));
  if (!match) throw new Error("[release-signing] installed application Team ID unavailable");
  return match[1];
}

function resolveMacIdentity(identityOutput, teamId, notBeforeByHash = {}) {
  const candidates = [];
  for (const match of String(identityOutput).matchAll(
    /^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(Developer ID Application: .+ \(([A-Z0-9]{10})\))"\s*$/gm,
  )) {
    const hash = match[1].toUpperCase();
    const identity = match[2];
    if (match[3] !== teamId) continue;
    candidates.push({ hash, identity, teamId, notBefore: String(notBeforeByHash[hash] || "") });
  }
  if (candidates.length === 0) throw new Error("[release-signing] no valid installed-Team Developer ID identity");
  candidates.sort((left, right) =>
    right.notBefore.localeCompare(left.notBefore) || right.hash.localeCompare(left.hash),
  );
  return candidates[0];
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
  signBundle = signDeveloperIdMacBundle,
} = {}) {
  const baseContract = macReleaseContract({ env, platform });
  const installedApp = path.join("/Applications", "EchoDesk.app");
  const teamMetadata = runChecked(
    runner,
    "codesign",
    ["--display", "--verbose=4", installedApp],
    { cwd: desktopRoot, env, capture: true, label: "installed Team ID preflight" },
  );
  const installedTeamId = installedMacTeamId(`${teamMetadata.stdout}\n${teamMetadata.stderr}`);
  const identityResult = runChecked(
    runner,
    "security",
    ["find-identity", "-v", "-p", "codesigning"],
    { cwd: desktopRoot, env, capture: true, label: "Developer ID identity preflight" },
  );
  const resolvedIdentity = resolveMacIdentity(
    `${identityResult.stdout}\n${identityResult.stderr}`,
    installedTeamId,
  );
  const contract = {
    ...baseContract,
    identity: resolvedIdentity.identity,
    signingHash: resolvedIdentity.hash,
    teamId: installedTeamId,
  };
  const releaseEnv = {
    ...env,
    ECHODESK_ADHOC_SIGN: "0",
    CSC_IDENTITY_AUTO_DISCOVERY: "false",
  };
  const commandOptions = { cwd: desktopRoot, env: releaseEnv };

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
      "dir",
      "--arm64",
      "--publish",
      "never",
      "--config.forceCodeSigning=false",
      "--config.mac.identity=null",
      "--config.mac.notarize=false",
      "--config.dmg.sign=false",
      "--config.electronDist=node_modules/electron/dist",
    ],
    { ...commandOptions, label: "Developer ID macOS package build" },
  );

  const artifacts = {
    app: path.join(desktopRoot, "release", "mac-arm64", "EchoDesk.app"),
  };
  ensureArtifactsExist(artifacts, exists);
  signBundle(artifacts.app, contract.signingHash, { timestamp: true, runner });
  for (const [label, artifactPath] of [["application", artifacts.app]]) {
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
  }

  // This build step never claims to have installed the candidate. The manifest
  // beside the final recovery archive is written only by canonical promotion,
  // after the canonical installed App has passed strict verification.

  logger.log("[release-signing] Verified Developer ID app-only bundle");
  return { contract, artifacts };
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

  const innerRoot = path.join(desktopRoot, "release", "win-unpacked");
  let innerPeFiles;
  if (existsSync(innerRoot)) {
    innerPeFiles = enumeratePeCoffFiles(innerRoot);
  } else if (exists === existsSync) {
    throw new Error(`[release-signing] Windows inner PE/COFF root is missing: ${innerRoot}`);
  } else {
    // Unit tests may inject a virtual exists() function without building a package.
    innerPeFiles = [
      { absolute_path: artifacts.application, relative_path: "EchoDesk.exe", size_bytes: null, sha256: null },
      { absolute_path: artifacts.backend, relative_path: path.join("resources", "backend", "echodesk-backend.exe"), size_bytes: null, sha256: null },
    ];
  }
  if (innerPeFiles.length === 0) {
    throw new Error(`[release-signing] No actual PE/COFF files found below ${innerRoot}`);
  }

  const verificationTargets = [
    ...innerPeFiles.map((record) => ({
      label: `inner PE/COFF ${record.relative_path}`,
      artifactPath: record.absolute_path,
      scope: "inner",
    })),
    { label: "outer NSIS installer", artifactPath: artifacts.installer, scope: "outer" },
  ];
  for (const { label, artifactPath } of verificationTargets) {
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
        label: `${label} Authenticode chain, SHA-256, and RFC 3161 verification`,
      },
    );
  }

  runChecked(
    runner,
    "pwsh",
    [
      ...verifierArgs,
      "-Mode",
      "VerifyZip",
      ...contractArgs,
      "-ArtifactPath",
      artifacts.zip,
    ],
    {
      ...commandOptions,
      capture: true,
      label: "portable ZIP Authenticode, SHA-256, and RFC 3161 verification",
    },
  );

  logger.log(
    `[release-signing] Verified ${innerPeFiles.length} inner PE/COFF files and 1 outer NSIS installer; portable ZIP was verified separately`,
  );
  return {
    contract,
    artifacts,
    windows_pe_scope: {
      inner_root: innerRoot,
      inner_pe_files: innerPeFiles,
      outer_pe_files: [{ relative_path: path.basename(artifacts.installer), absolute_path: artifacts.installer }],
      portable_zip: artifacts.zip,
      verification: "each inner PE/COFF, portable ZIP PE/COFF, and outer NSIS installer verified individually",
    },
  };
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
  assertMacSignatureMetadata,
  assertNotaryAccepted,
  macReleaseContract,
  installedMacTeamId,
  resolveMacIdentity,
  normalizeCertificateThumbprint,
  runMacRelease,
  runWindowsRelease,
  windowsReleaseContract,
};

/* eslint-disable no-console */
const { readFileSync } = require("node:fs");
const path = require("node:path");

const SCHEMA_VERSION = 1;
const CONTRACT_ID = "echodesk.b12.signing-scope";
const STARTING_SHA = "ffbacb9d0ffa1b62a205f98ff437be4219e9ee08";
const DEFAULT_APP_ID = "com.echodesk.app";
const DEFAULT_ELECTRON_VERSION = "43.1.0";

function assertNonEmpty(value, field) {
  const text = String(value || "").trim();
  if (!text || /[\0\r\n]/.test(text)) {
    throw new Error(`[b12-signing-scope] ${field} must be a non-empty single-line value`);
  }
  return text;
}

function assertSha(value, field) {
  const sha = assertNonEmpty(value, field).toLowerCase();
  if (!/^[0-9a-f]{40}$/.test(sha)) {
    throw new Error(`[b12-signing-scope] ${field} must be a full 40-character SHA-1`);
  }
  return sha;
}

function assertVersion(value, field) {
  const version = assertNonEmpty(value, field);
  if (!/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(version)) {
    throw new Error(`[b12-signing-scope] ${field} contains unsupported characters`);
  }
  return version;
}

function readDesktopPackage(desktopRoot) {
  const packagePath = path.join(desktopRoot, "..", "package.json");
  try {
    const packageJson = JSON.parse(readFileSync(packagePath, "utf8"));
    return {
      version: packageJson.version,
      electronVersion: packageJson.devDependencies?.electron,
    };
  } catch {
    return {};
  }
}

function protectedInput(name, rationale) {
  return Object.freeze({
    name,
    source: "protected-signing-environment",
    value: "not-read-by-B12",
    rationale,
  });
}

function createSigningScope({
  releaseSha,
  startingSha = STARTING_SHA,
  echoVersion = "unknown",
  electronVersion = DEFAULT_ELECTRON_VERSION,
  appId = DEFAULT_APP_ID,
  platforms = ["macos-arm64", "windows-x64"],
} = {}) {
  const normalizedReleaseSha = assertSha(releaseSha, "releaseSha");
  const normalizedStartingSha = assertSha(startingSha, "startingSha");
  const normalizedEchoVersion = assertVersion(echoVersion, "echoVersion");
  const normalizedElectronVersion = assertVersion(electronVersion, "electronVersion");
  const normalizedAppId = assertNonEmpty(appId, "appId");
  if (!Array.isArray(platforms) || platforms.length === 0) {
    throw new Error("[b12-signing-scope] platforms must be a non-empty array");
  }
  const normalizedPlatforms = platforms.map((platform) => assertNonEmpty(platform, "platform"));

  return {
    schema_version: SCHEMA_VERSION,
    contract_id: CONTRACT_ID,
    scope_owner: "B12-signing-scope-readback-runner",
    release_sha: normalizedReleaseSha,
    immutable_starting_sha: normalizedStartingSha,
    echo_version: normalizedEchoVersion,
    electron_version: normalizedElectronVersion,
    app_bundle_identifier: normalizedAppId,
    platforms: normalizedPlatforms,
    credential_policy: {
      credentials_read: false,
      credentials_written: false,
      credential_values_embedded: false,
      signing_execution: false,
      notarization_execution: false,
      timestamp_service_execution: false,
      protected_inputs_only: true,
    },
    macos: {
      platform: "darwin",
      architectures: ["arm64"],
      minimum_os: "protected-config:macOS-minimum-version",
      team_id: "protected-config:Apple-Team-ID",
      bundle_identifier: normalizedAppId,
      hardened_runtime: true,
      signing_identity: protectedInput(
        "Developer ID Application",
        "B14 supplies the exact protected identity; B12 must not discover or read it",
      ),
      notarization: protectedInput(
        "Apple notarization profile",
        "B14 supplies the protected profile after content freeze",
      ),
      nested_code_scope: [
        {
          kind: "main-executable",
          path: "EchoDesk.app/Contents/MacOS/EchoDesk",
          required: true,
        },
        {
          kind: "electron-helper-app",
          path: "EchoDesk.app/Contents/Frameworks/**/*.app/**",
          required: true,
        },
        {
          kind: "native-module",
          path: "EchoDesk.app/Contents/Frameworks/**/*.dylib|*.node",
          required: true,
        },
        {
          kind: "bundled-backend-executable",
          path: "EchoDesk.app/Contents/Resources/backend/<platform-backend>",
          required: true,
        },
        {
          kind: "agent-runtime-native-executable",
          path: "EchoDesk.app/Contents/Resources/agent-runtime/native/<platform>-<arch>/**",
          required: false,
          note: "Only when the frozen fusion manifest lists a native executable",
        },
      ],
      entitlements: {
        main_file: "protected-config:macos-entitlements.plist",
        inherit_file: "protected-config:macos-entitlements-inherit.plist",
        hardened_runtime_required: true,
        allowed: [
          {
            key: "com.apple.security.cs.allow-jit",
            default: false,
            condition: "only with a frozen runtime proof that JIT is required",
          },
          {
            key: "com.apple.security.cs.disable-library-validation",
            default: false,
            condition: "only with a frozen native-module proof",
          },
        ],
        prohibited_without_explicit_proof: [
          "com.apple.security.cs.allow-unsigned-executable-memory",
          "com.apple.security.cs.allow-dyld-environment-variables",
        ],
        inherit_must_not_widen_main: true,
      },
      order: [
        "freeze logical content and fusion-content-manifest",
        "sign nested Mach-O/native code inside-out",
        "sign main EchoDesk.app with explicit entitlements",
        "sign outer DMG container",
        "notarize DMG through protected workflow",
        "staple only after notarization Accepted",
        "verify signatures and read back logical content",
        "refresh updater metadata from final signed artifact only",
      ],
      final_readback_inputs: [".app", ".dmg", "updater ZIP", "installed app directory"],
    },
    windows: {
      platform: "win32",
      architectures: ["x64"],
      signing_algorithm: "sha256",
      certificate: {
        thumbprint: protectedInput(
          "Authenticode certificate thumbprint",
          "B15 supplies the protected certificate selector",
        ),
        publisher: protectedInput(
          "Authenticode publisher",
          "B15 verifies the exact publisher and chain",
        ),
      },
      timestamp: protectedInput(
        "RFC 3161 timestamp URL",
        "B15 uses the protected timestamp configuration",
      ),
      pe_scope: {
        inner: [
          "win-unpacked/EchoDesk.exe",
          "win-unpacked/resources/backend/echodesk-backend.exe",
          "win-unpacked/resources/**/*.dll",
          "win-unpacked/resources/**/*.node",
          "win-unpacked/resources/**/helper*.exe",
          "win-unpacked/resources/**/updater*.exe",
          "win-unpacked/resources/**/uninstaller*.exe",
        ],
        outer: ["EchoDesk.Setup.<version>.exe"],
        portable_container: "EchoDesk-<version>-win-x64.zip contains only already-signed PE bytes",
      },
      order: [
        "freeze logical content and fusion-content-manifest",
        "enumerate and sign every inner PE/COFF executable",
        "verify inner Authenticode chain, publisher, SHA-256 and RFC 3161 timestamp",
        "build portable ZIP from verified inner bytes",
        "sign outer NSIS installer",
        "verify outer installer chain, publisher, SHA-256 and RFC 3161 timestamp",
        "install/read back without patching package bytes",
        "refresh updater metadata from final signed artifact only",
      ],
      final_readback_inputs: ["NSIS installed directory", "portable ZIP", "installed directory"],
    },
    forbidden_fallbacks: [
      "external Claude CLI",
      "external AgentOS CLI/daemon/runtime",
      "runtime package-manager installation",
      "PATH-based runtime discovery",
      "HOME/global-auth fallback",
    ],
    required_readback_status: "release_blocked_signing_on_any_mismatch",
    generated_by: "B12 production contract only; no signing command is executed",
  };
}

function validateSigningScope(scope) {
  if (!scope || typeof scope !== "object") throw new Error("[b12-signing-scope] scope must be an object");
  if (scope.schema_version !== SCHEMA_VERSION || scope.contract_id !== CONTRACT_ID) {
    throw new Error("[b12-signing-scope] unsupported signing scope schema");
  }
  assertSha(scope.release_sha, "scope.release_sha");
  assertSha(scope.immutable_starting_sha, "scope.immutable_starting_sha");
  if (scope.credential_policy?.credentials_read !== false || scope.credential_policy?.signing_execution !== false) {
    throw new Error("[b12-signing-scope] B12 scope cannot read credentials or execute signing");
  }
  for (const platform of ["macos", "windows"]) {
    if (!scope[platform] || !Array.isArray(scope[platform].order) || scope[platform].order.length === 0) {
      throw new Error(`[b12-signing-scope] ${platform} signing order is missing`);
    }
  }
  const serialized = JSON.stringify(scope);
  if (/(?:password|secret|private[_ -]?key|pfx|p12|notary[_ -]?token|auth[_ -]?token)\s*[:=]/i.test(serialized)) {
    throw new Error("[b12-signing-scope] credential-shaped values are forbidden in the scope");
  }
  return scope;
}

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) throw new Error(`[b12-signing-scope] unexpected argument ${token}`);
    const key = token.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`[b12-signing-scope] ${token} requires a value`);
    args[key] = value;
    index += 1;
  }
  return args;
}

function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const packageInfo = readDesktopPackage(path.resolve(__dirname));
  const scope = validateSigningScope(
    createSigningScope({
      releaseSha: args.releaseSha,
      echoVersion: args.echoVersion || packageInfo.version || "unknown",
      electronVersion: args.electronVersion || packageInfo.electronVersion || DEFAULT_ELECTRON_VERSION,
      appId: args.appId || DEFAULT_APP_ID,
      platforms: args.platforms ? args.platforms.split(",") : undefined,
    }),
  );
  process.stdout.write(`${JSON.stringify(scope, null, 2)}\n`);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 2;
  }
}

module.exports = {
  CONTRACT_ID,
  SCHEMA_VERSION,
  STARTING_SHA,
  createSigningScope,
  validateSigningScope,
};

/* eslint-disable @typescript-eslint/no-var-requires */

const { existsSync, readFileSync, writeFileSync } = require("node:fs");
const { homedir } = require("node:os");
const { basename, join, resolve } = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = join(__dirname, "..");
const ANDROID_DIR = join(ROOT, "android");
const RELEASE_DIR = process.env.ECHODESK_RELEASE_DIR
  ? resolve(process.env.ECHODESK_RELEASE_DIR)
  : join(ROOT, "release");
const WEB_INDEX_PATH = join(
  ANDROID_DIR,
  "app",
  "src",
  "main",
  "assets",
  "public",
  "index.html",
);
const TV_RUNTIME_MARKER = "__ECHODESK_TV_PACKAGE__";
const ROTATION_MIN_SDK_VERSION = 33;

function firstExisting(paths) {
  return paths.find((candidate) => candidate && existsSync(candidate)) || null;
}

function resolveJavaHome(env = process.env) {
  return firstExisting([
    env.JAVA_HOME,
    "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
    "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home",
    "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
  ]);
}

function resolveAndroidHome(env = process.env) {
  return firstExisting([
    env.ANDROID_HOME,
    env.ANDROID_SDK_ROOT,
    join(homedir(), "Library", "Android", "sdk"),
  ]);
}

function androidEnvironment(env = process.env) {
  const javaHome = resolveJavaHome(env);
  const androidHome = resolveAndroidHome(env);
  if (!javaHome) {
    throw new Error(
      "Android build failed: JAVA_HOME not found. Install Android Studio or export JAVA_HOME.",
    );
  }
  if (!androidHome) {
    throw new Error(
      "Android build failed: Android SDK not found. Install Android SDK or export ANDROID_HOME.",
    );
  }
  return {
    ...env,
    JAVA_HOME: javaHome,
    ANDROID_HOME: androidHome,
    ANDROID_SDK_ROOT: androidHome,
    PATH: `${join(javaHome, "bin")}:${join(androidHome, "platform-tools")}:${join(androidHome, "emulator")}:${env.PATH || ""}`,
  };
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || ROOT,
    env: options.env || process.env,
    encoding: options.capture ? "utf8" : undefined,
    stdio: options.capture ? "pipe" : "inherit",
    shell: false,
  });
  if (result.error || result.status !== 0) {
    const detail = options.capture
      ? `${result.stdout || ""}${result.stderr || ""}`.trim()
      : "";
    throw result.error || new Error(
      `${command} exited with ${result.status}${detail ? `: ${detail}` : ""}`,
    );
  }
  return options.capture ? `${result.stdout || ""}${result.stderr || ""}` : "";
}

function resolveBuildTool(androidHome, name) {
  const versions = ["36.1.0", "36.0.0", "35.0.0", "34.0.0"];
  const tool = firstExisting(
    versions.map((version) => join(androidHome, "build-tools", version, name)),
  );
  if (!tool) {
    throw new Error(`${name} not found in Android SDK build-tools`);
  }
  return tool;
}

function patchTvRuntimeMarker() {
  const original = readFileSync(WEB_INDEX_PATH, "utf8");
  if (original.includes(TV_RUNTIME_MARKER)) {
    return () => writeFileSync(WEB_INDEX_PATH, original, "utf8");
  }
  const marker = [
    "<script>",
    "window.__ECHODESK_TV_PACKAGE__=true;",
    "try{localStorage.setItem('echodesk.forceTvUi','1')}catch(e){}",
    "</script>",
  ].join("");
  const patched = original.includes("<head>")
    ? original.replace("<head>", `<head>${marker}`)
    : `${marker}${original}`;
  writeFileSync(WEB_INDEX_PATH, patched, "utf8");
  return () => writeFileSync(WEB_INDEX_PATH, original, "utf8");
}

function normalizeFingerprint(raw) {
  const value = String(raw || "").replace(/[^0-9a-f]/gi, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(value)) {
    throw new Error("release certificate SHA-256 must contain exactly 64 hex digits");
  }
  return value;
}

function releaseSigningContract(env = process.env) {
  const required = [
    "ECHODESK_ANDROID_LEGACY_KEYSTORE",
    "ECHODESK_ANDROID_LEGACY_KEY_ALIAS",
    "ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD",
    "ECHODESK_ANDROID_LEGACY_KEY_PASSWORD",
    "ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256",
    "ECHODESK_ANDROID_CURRENT_KEYSTORE",
    "ECHODESK_ANDROID_CURRENT_KEY_ALIAS",
    "ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD",
    "ECHODESK_ANDROID_CURRENT_KEY_PASSWORD",
    "ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256",
    "ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION",
  ];
  const missing = required.filter((name) => !String(env[name] || "").trim());
  if (missing.length) {
    throw new Error(
      `public Android release requires legacy/current signing inputs: ${missing.join(", ")}`,
    );
  }
  const rotationMinSdkVersion = Number.parseInt(
    String(env.ECHODESK_ANDROID_ROTATION_MIN_SDK_VERSION),
    10,
  );
  if (rotationMinSdkVersion !== ROTATION_MIN_SDK_VERSION) {
    throw new Error(
      `public Android release requires rotation min SDK ${ROTATION_MIN_SDK_VERSION}`,
    );
  }

  const legacy = {
    keystore: String(env.ECHODESK_ANDROID_LEGACY_KEYSTORE),
    alias: String(env.ECHODESK_ANDROID_LEGACY_KEY_ALIAS),
    keystorePasswordEnv: "ECHODESK_ANDROID_LEGACY_KEYSTORE_PASSWORD",
    keyPasswordEnv: "ECHODESK_ANDROID_LEGACY_KEY_PASSWORD",
    expectedFingerprint: normalizeFingerprint(
      env.ECHODESK_ANDROID_EXPECTED_LEGACY_CERT_SHA256,
    ),
  };
  const current = {
    keystore: String(env.ECHODESK_ANDROID_CURRENT_KEYSTORE),
    alias: String(env.ECHODESK_ANDROID_CURRENT_KEY_ALIAS),
    keystorePasswordEnv: "ECHODESK_ANDROID_CURRENT_KEYSTORE_PASSWORD",
    keyPasswordEnv: "ECHODESK_ANDROID_CURRENT_KEY_PASSWORD",
    expectedFingerprint: normalizeFingerprint(
      env.ECHODESK_ANDROID_EXPECTED_CURRENT_CERT_SHA256,
    ),
  };
  for (const [role, signer] of Object.entries({ legacy, current })) {
    if (!existsSync(signer.keystore)) {
      throw new Error(`${role} release keystore does not exist: ${signer.keystore}`);
    }
  }
  if (
    basename(current.keystore).toLowerCase() === "debug.keystore" ||
    current.alias.toLowerCase() === "androiddebugkey"
  ) {
    throw new Error("the current public release signer must not be a debug identity");
  }
  if (legacy.expectedFingerprint === current.expectedFingerprint) {
    throw new Error("legacy and current release certificate fingerprints must differ");
  }

  return {
    legacy,
    current,
    rotationMinSdkVersion,
  };
}

function apksignerSignerArgs(signer) {
  return [
    "--ks",
    signer.keystore,
    "--ks-key-alias",
    signer.alias,
    "--ks-pass",
    `env:${signer.keystorePasswordEnv}`,
    "--key-pass",
    `env:${signer.keyPasswordEnv}`,
  ];
}

function readKeystoreFingerprint(signer, env = process.env) {
  const javaHome = resolveJavaHome(env);
  if (!javaHome) throw new Error("JAVA_HOME is required to inspect Android release keys");
  const keytool = join(javaHome, "bin", process.platform === "win32" ? "keytool.exe" : "keytool");
  if (!existsSync(keytool)) throw new Error(`keytool does not exist: ${keytool}`);
  const output = run(
    keytool,
    [
      "-J-Duser.language=en",
      "-J-Duser.country=US",
      "-list",
      "-v",
      "-keystore",
      signer.keystore,
      "-alias",
      signer.alias,
      "-storepass:env",
      signer.keystorePasswordEnv,
    ],
    { env, capture: true },
  );
  const match = output.match(/SHA256:\s*([0-9a-f:]+)/i);
  if (!match) {
    throw new Error(`keytool did not report a SHA-256 fingerprint for alias ${signer.alias}`);
  }
  return normalizeFingerprint(match[1]);
}

function verifySigningIdentities(signing, env = process.env) {
  const legacyFingerprint = readKeystoreFingerprint(signing.legacy, env);
  const currentFingerprint = readKeystoreFingerprint(signing.current, env);
  if (legacyFingerprint !== signing.legacy.expectedFingerprint) {
    throw new Error(
      `legacy release certificate fingerprint mismatch: expected ${signing.legacy.expectedFingerprint}, got ${legacyFingerprint}`,
    );
  }
  if (currentFingerprint !== signing.current.expectedFingerprint) {
    throw new Error(
      `current release certificate fingerprint mismatch: expected ${signing.current.expectedFingerprint}, got ${currentFingerprint}`,
    );
  }
  if (legacyFingerprint === currentFingerprint) {
    throw new Error("legacy and current Android release signers resolve to the same certificate");
  }
  return { legacyFingerprint, currentFingerprint };
}

module.exports = {
  ANDROID_DIR,
  RELEASE_DIR,
  ROTATION_MIN_SDK_VERSION,
  ROOT,
  androidEnvironment,
  apksignerSignerArgs,
  normalizeFingerprint,
  patchTvRuntimeMarker,
  readKeystoreFingerprint,
  releaseSigningContract,
  resolveBuildTool,
  run,
  verifySigningIdentities,
};

/* eslint-disable @typescript-eslint/no-var-requires, no-undef */
const {
  constants,
  accessSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

if (process.platform !== "darwin") {
  throw new Error("[dmg-smoke] mounted DMG smoke requires macOS");
}

const desktopRoot = path.resolve(__dirname, "..");
const releaseRoot = path.join(desktopRoot, "release");
const packageVersion = String(
  JSON.parse(readFileSync(path.join(desktopRoot, "package.json"), "utf8"))
    .version || "",
).trim();
if (!packageVersion) throw new Error("[dmg-smoke] package version is missing");

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function resolveDmg() {
  const configured = process.env.ECHODESK_DMG?.trim();
  if (configured) return path.resolve(configured);
  if (!existsSync(releaseRoot)) {
    throw new Error(
      `[dmg-smoke] release directory does not exist: ${releaseRoot}`,
    );
  }
  const versionedDmg = new RegExp(
    `^EchoDesk-${escapeRegExp(packageVersion)}-.*\\.dmg$`,
    "i",
  );
  const candidates = readdirSync(releaseRoot)
    .filter((name) => versionedDmg.test(name))
    .map((name) => path.join(releaseRoot, name))
    .sort((left, right) => statSync(right).mtimeMs - statSync(left).mtimeMs);
  if (!candidates.length) {
    throw new Error(
      `[dmg-smoke] no EchoDesk ${packageVersion} DMG found in ${releaseRoot}`,
    );
  }
  return candidates[0];
}

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    encoding: "utf8",
    ...options,
  });
}

const dmg = resolveDmg();
if (!existsSync(dmg) || !statSync(dmg).isFile()) {
  throw new Error(`[dmg-smoke] DMG does not exist: ${dmg}`);
}

const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-dmg-smoke-"));
const mountPoint = path.join(root, "mount");
const home = path.join(root, "home");
const temp = path.join(root, "tmp");
const state = path.join(root, "state");
for (const directory of [mountPoint, home, temp, state]) {
  mkdirSync(directory, { recursive: true });
}

let mounted = false;
let smokeStatus = 1;
try {
  console.log(`[dmg-smoke] mounting read-only ${dmg}`);
  const attach = run(
    "/usr/bin/hdiutil",
    ["attach", "-readonly", "-nobrowse", "-mountpoint", mountPoint, dmg],
    { stdio: "inherit" },
  );
  if (attach.error || attach.status !== 0) {
    throw (
      attach.error ||
      new Error(`[dmg-smoke] hdiutil attach exited with ${attach.status}`)
    );
  }
  mounted = true;

  const writeProbe = path.join(mountPoint, ".echodesk-write-probe");
  try {
    writeFileSync(writeProbe, "must fail");
    unlinkSync(writeProbe);
    throw new Error("[dmg-smoke] mount accepted a write despite -readonly");
  } catch (error) {
    if (error?.code !== "EROFS" && error?.code !== "EACCES") throw error;
  }

  const appName = readdirSync(mountPoint).find((name) => name.endsWith(".app"));
  if (!appName)
    throw new Error(`[dmg-smoke] no app bundle found at ${mountPoint}`);
  const appBin = path.join(
    mountPoint,
    appName,
    "Contents",
    "MacOS",
    "EchoDesk",
  );
  const backendBin = path.join(
    mountPoint,
    appName,
    "Contents",
    "Resources",
    "backend",
    "echodesk-backend",
  );
  accessSync(appBin, constants.X_OK);
  accessSync(backendBin, constants.X_OK);

  const env = {
    ...process.env,
    HOME: home,
    TMPDIR: temp,
    ECHODESK_APP_BIN: appBin,
    ECHODESK_EXPECTED_BACKEND_BIN: backendBin,
    ECHODESK_SMOKE_ROOT: state,
    ECHODESK_SMOKE_HOME: home,
    ECHODESK_DMG_MOUNT: mountPoint,
    ECHODESK_REQUIRE_MOUNTED_DMG: "1",
    ECHODESK_EXPECTED_VERSION: packageVersion,
    ECHODESK_SMOKE_PORT: process.env.ECHODESK_SMOKE_PORT || "18769",
    ECHO_FORCE_LOCAL_BACKEND: "1",
    ECHODESK_NODE_RUNTIME: appBin,
    ECHODESK_NODE_RUNTIME_IS_ELECTRON: "1",
  };
  for (const name of [
    "ECHO_PYTHON",
    "ECHO_BACKEND_CWD",
    "ECHO_ALLOW_PACKAGED_SOURCE_BACKEND",
    "ECHO_USER_DIR",
    "DB_PATH",
    "STORAGE_DIR",
    "RAG_INDEX_DIR",
    "SKILL_EXECUTOR_BUILD_DIR",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "ELECTRON_DEV",
    "VITE_DEV_URL",
    "ECHO_PUBLIC_DEMO",
    "ECHO_SPAWN_BACKEND",
  ]) {
    delete env[name];
  }

  const artifactRuntimeRoot = path.join(state, "artifact-runtime");
  mkdirSync(artifactRuntimeRoot, { recursive: true });
  const artifactSmoke = run(
    backendBin,
    ["--artifact-runtime-smoke", artifactRuntimeRoot],
    { env, stdio: "inherit" },
  );
  if (artifactSmoke.error || artifactSmoke.status !== 0) {
    throw (
      artifactSmoke.error ||
      new Error(
        `[dmg-smoke] packaged artifact runtime exited with ${artifactSmoke.status}`,
      )
    );
  }
  const artifactManifestPath = path.join(
    artifactRuntimeRoot,
    "artifact-runtime-smoke.json",
  );
  const artifactManifest = JSON.parse(
    readFileSync(artifactManifestPath, "utf8"),
  );
  if (artifactManifest.ok !== true) {
    throw new Error(
      "[dmg-smoke] packaged artifact runtime did not report success",
    );
  }
  for (const kind of ["docx", "xlsx", "pdf", "pptx"]) {
    const artifact = artifactManifest.artifacts?.[kind];
    if (
      !artifact ||
      !existsSync(artifact.path) ||
      !Number.isFinite(artifact.size_bytes) ||
      artifact.size_bytes <= 100
    ) {
      throw new Error(
        `[dmg-smoke] packaged ${kind} runtime artifact is invalid`,
      );
    }
  }
  for (const kind of ["docx", "xlsx", "pdf", "pptx", "html", "csv", "epub"]) {
    if (!Number.isFinite(artifactManifest.rag_parser_chars?.[kind]) ||
        artifactManifest.rag_parser_chars[kind] <= 0) {
      throw new Error(`[dmg-smoke] packaged ${kind} RAG parser is unavailable`);
    }
  }

  const playwrightCli = path.join(
    desktopRoot,
    "node_modules",
    "@playwright",
    "test",
    "cli.js",
  );
  if (!existsSync(playwrightCli)) {
    throw new Error(`[dmg-smoke] Playwright CLI missing: ${playwrightCli}`);
  }
  const smoke = run(
    process.execPath,
    [
      playwrightCli,
      "test",
      "--config=playwright.real.config.ts",
      "tests/e2e-real/packaged-local-smoke.spec.ts",
      "--reporter=line",
      "--workers=1",
    ],
    { cwd: desktopRoot, env, stdio: "inherit" },
  );
  smokeStatus = smoke.status ?? 1;
  if (smoke.error || smokeStatus !== 0) {
    throw (
      smoke.error ||
      new Error(`[dmg-smoke] Playwright exited with ${smokeStatus}`)
    );
  }
  console.log(
    `[dmg-smoke] PASS bundled backend from read-only DMG: ${backendBin}`,
  );
} finally {
  if (mounted) {
    let detach = run("/usr/bin/hdiutil", ["detach", mountPoint], {
      stdio: "inherit",
    });
    if (detach.error || detach.status !== 0) {
      detach = run("/usr/bin/hdiutil", ["detach", "-force", mountPoint], {
        stdio: "inherit",
      });
      if (detach.error || detach.status !== 0) {
        console.error(`[dmg-smoke] failed to detach ${mountPoint}`);
      }
    }
  }
  rmSync(root, { recursive: true, force: true });
}

process.exitCode = smokeStatus;

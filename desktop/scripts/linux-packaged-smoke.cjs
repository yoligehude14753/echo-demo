/* eslint-disable @typescript-eslint/no-var-requires, no-undef */
const {
  constants,
  accessSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync, spawn, spawnSync } = require("node:child_process");

if (process.platform !== "linux" || process.arch !== "x64") {
  throw new Error("[linux-smoke] requires x64 Linux");
}

const desktopRoot = path.resolve(__dirname, "..");
const packageVersion = String(
  JSON.parse(readFileSync(path.join(desktopRoot, "package.json"), "utf8"))
    .version || "",
).trim();
if (!packageVersion)
  throw new Error("[linux-smoke] package version is missing");

const appBin = path.resolve(
  process.env.ECHODESK_APP_BIN ||
    path.join(desktopRoot, "release", "linux-unpacked", "echodesk"),
);
const backendBin = path.resolve(
  process.env.ECHODESK_EXPECTED_BACKEND_BIN ||
    path.join(
      desktopRoot,
      "release",
      "linux-unpacked",
      "resources",
      "backend",
      "echodesk-backend",
    ),
);
const port = Number(process.env.ECHODESK_SMOKE_PORT || "18769");
if (!Number.isInteger(port) || port < 1 || port > 65_535) {
  throw new Error(`[linux-smoke] invalid smoke port: ${port}`);
}
const smokeRoot = path.resolve(
  process.env.ECHODESK_SMOKE_ROOT ||
    path.join(os.tmpdir(), `echodesk-linux-packaged-smoke-${process.pid}`),
);
const home = path.join(smokeRoot, "home");
const runtimeDir = path.join(smokeRoot, "runtime");
const dbPath = path.join(smokeRoot, "echodesk.db");

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function poll(description, predicate, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (error) {
      if (error?.fatal === true) throw error;
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(
    `[linux-smoke] timed out waiting for ${description}${
      lastError ? `: ${lastError.message}` : ""
    }`,
  );
}

function fatalError(message, cause) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.fatal = true;
  return error;
}

async function portOpen() {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/healthz`, {
      signal: AbortSignal.timeout(1_000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function jsonRequest(pathname, init = {}) {
  const response = await fetch(`http://127.0.0.1:${port}${pathname}`, {
    ...init,
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) {
    throw new Error(`${pathname} returned HTTP ${response.status}`);
  }
  return response.json();
}

function descendantCommands(parentPid) {
  const output = execFileSync("/bin/ps", ["-axo", "ppid=,pid=,command="], {
    encoding: "utf8",
  });
  const rows = output
    .split("\n")
    .map((line) => line.match(/^\s*(\d+)\s+(\d+)\s+(.*)$/))
    .filter(Boolean)
    .map((match) => ({
      ppid: Number(match[1]),
      pid: Number(match[2]),
      command: match[3],
    }));
  const descendants = new Set([parentPid]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (descendants.has(row.ppid) && !descendants.has(row.pid)) {
        descendants.add(row.pid);
        changed = true;
      }
    }
  }
  return rows
    .filter((row) => descendants.has(row.pid))
    .map((row) => row.command);
}

function isolatedEnvironment() {
  const env = {
    ...process.env,
    HOME: home,
    XDG_RUNTIME_DIR: runtimeDir,
    XDG_CONFIG_HOME: path.join(smokeRoot, "config"),
    XDG_CACHE_HOME: path.join(smokeRoot, "cache"),
    ECHO_BACKEND_PORT: String(port),
    ECHO_BACKEND_BIND_HOST: "127.0.0.1",
    ECHO_FORCE_LOCAL_BACKEND: "1",
    ECHO_SPAWN_BACKEND: "1",
    ECHO_USER_DIR: path.join(smokeRoot, "user"),
    DB_PATH: dbPath,
    STORAGE_DIR: path.join(smokeRoot, "storage"),
    RAG_INDEX_DIR: path.join(smokeRoot, "rag"),
    SKILL_EXECUTOR_BUILD_DIR: path.join(smokeRoot, "skill-build"),
    WORKSPACE_SCAN_ON_STARTUP: "false",
    DIARIZER_ENABLED: "false",
    TTS_ENABLED: "false",
    AGENT_OS_ENABLED: "false",
    ECHODESK_DISABLE_AUTO_UPDATE_DOWNLOAD: "1",
    ECHODESK_AUTO_UPDATE_CHECK_DELAY_MS: "3600000",
    ECHODESK_NODE_RUNTIME: appBin,
    ECHODESK_NODE_RUNTIME_IS_ELECTRON: "1",
    LIBGL_ALWAYS_SOFTWARE: "1",
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
  };
  for (const name of [
    "ECHO_PYTHON",
    "ECHO_BACKEND_CWD",
    "ECHO_ALLOW_PACKAGED_SOURCE_BACKEND",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "ELECTRON_DEV",
    "VITE_DEV_URL",
    "ECHO_PUBLIC_DEMO",
  ]) {
    delete env[name];
  }
  return env;
}

function validateArtifactRuntime(env) {
  const outputDir = path.join(smokeRoot, "artifact-runtime");
  mkdirSync(outputDir, { recursive: true });
  const result = spawnSync(
    backendBin,
    ["--artifact-runtime-smoke", outputDir],
    {
      cwd: path.dirname(backendBin),
      env,
      encoding: "utf8",
      timeout: 180_000,
    },
  );
  if (result.error || result.status !== 0) {
    throw (
      result.error ||
      new Error(
        `[linux-smoke] artifact runtime failed (${result.status}): ${
          result.stderr || result.stdout || "no output"
        }`,
      )
    );
  }
  const manifest = JSON.parse(
    readFileSync(path.join(outputDir, "artifact-runtime-smoke.json"), "utf8"),
  );
  if (manifest.ok !== true) {
    throw new Error("[linux-smoke] artifact runtime did not report success");
  }
  for (const kind of ["docx", "xlsx", "pdf", "pptx"]) {
    const artifact = manifest.artifacts?.[kind];
    if (
      !artifact ||
      !existsSync(artifact.path) ||
      !Number.isFinite(artifact.size_bytes) ||
      artifact.size_bytes <= 100
    ) {
      throw new Error(`[linux-smoke] invalid packaged ${kind} artifact`);
    }
  }
  console.log(
    `[linux-smoke] artifact runtime PASS ${JSON.stringify(
      Object.fromEntries(
        Object.entries(manifest.artifacts).map(([kind, value]) => [
          kind,
          value.size_bytes,
        ]),
      ),
    )}`,
  );
}

function launchApp(env, label) {
  const stdoutPath = path.join(smokeRoot, `${label}.stdout.log`);
  const stderrPath = path.join(smokeRoot, `${label}.stderr.log`);
  const stdout = openSync(stdoutPath, "w");
  const stderr = openSync(stderrPath, "w");
  const child = spawn(
    "xvfb-run",
    [
      "--auto-servernum",
      "--server-args=-screen 0 1280x800x24",
      appBin,
      "--no-sandbox",
      "--disable-gpu",
      `--user-data-dir=${path.join(smokeRoot, "electron")}`,
    ],
    {
      cwd: path.dirname(appBin),
      env,
      detached: true,
      stdio: ["ignore", stdout, stderr],
    },
  );
  const run = { child, stdoutPath, stderrPath, spawnError: null };
  let logsClosed = false;
  const closeLogs = () => {
    if (logsClosed) return;
    logsClosed = true;
    closeSync(stdout);
    closeSync(stderr);
  };
  child.once("error", (error) => {
    run.spawnError = error;
  });
  child.once("close", closeLogs);
  return run;
}

async function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.removeListener("close", onClose);
      resolve(false);
    }, timeoutMs);
    const onClose = () => {
      clearTimeout(timer);
      resolve(true);
    };
    child.once("close", onClose);
  });
}

async function stopApp(run) {
  const { child } = run;
  if (child.exitCode === null && child.pid) {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {}
    if (!(await waitForExit(child, 15_000))) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch {}
      await waitForExit(child, 5_000);
    }
  }
  await poll("backend port cleanup", async () => !(await portOpen()), 30_000);
}

function printLogs(run) {
  for (const logPath of [run?.stdoutPath, run?.stderrPath]) {
    if (!logPath || !existsSync(logPath)) continue;
    const text = readFileSync(logPath, "utf8").trim();
    if (text) console.error(`[linux-smoke] ${path.basename(logPath)}\n${text}`);
  }
}

async function validateRunningApp(run) {
  const health = await poll("bundled backend health", async () => {
    if (run.spawnError) {
      throw fatalError(
        `[linux-smoke] failed to launch xvfb-run: ${run.spawnError.message}`,
        run.spawnError,
      );
    }
    if (run.child.exitCode !== null || run.child.signalCode !== null) {
      throw fatalError(
        `[linux-smoke] Electron exited before backend health ` +
          `(code=${run.child.exitCode}, signal=${run.child.signalCode})`,
      );
    }
    try {
      return await jsonRequest("/healthz");
    } catch {
      return null;
    }
  });
  if (health.status !== "ok" || health.version !== packageVersion) {
    throw new Error(`[linux-smoke] health mismatch: ${JSON.stringify(health)}`);
  }
  const bootstrap = await jsonRequest("/bootstrap");
  if (
    bootstrap.backend_version !== packageVersion ||
    bootstrap.app_version !== packageVersion ||
    bootstrap.api_version !== "0.3"
  ) {
    throw new Error(
      `[linux-smoke] bootstrap mismatch: ${JSON.stringify(bootstrap)}`,
    );
  }
  await poll("actual bundled backend child", async () =>
    descendantCommands(run.child.pid).some((command) =>
      command.includes(backendBin),
    ),
  );
  if (run.child.exitCode !== null || run.child.signalCode !== null) {
    throw new Error(
      `[linux-smoke] Electron exited after startup ` +
        `(code=${run.child.exitCode}, signal=${run.child.signalCode})`,
    );
  }
}

async function main() {
  accessSync(appBin, constants.X_OK);
  accessSync(backendBin, constants.X_OK);
  if (!statSync(appBin).isFile() || !statSync(backendBin).isFile()) {
    throw new Error("[linux-smoke] packaged executables must be regular files");
  }
  rmSync(smokeRoot, { recursive: true, force: true });
  for (const directory of [home, runtimeDir]) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  await poll("unused smoke port", async () => !(await portOpen()), 5_000);
  const env = isolatedEnvironment();
  validateArtifactRuntime(env);

  let run = launchApp(env, "first-launch");
  try {
    await validateRunningApp(run);
    const meetingId = "linux-packaged-smoke-meeting";
    await jsonRequest(`/meetings/${meetingId}/start`, { method: "POST" });
    await jsonRequest(`/meetings/${meetingId}/inject_segment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: "linux packaged smoke durable segment",
        start_ms: 0,
        end_ms: 1_000,
      }),
    });
  } catch (error) {
    printLogs(run);
    throw error;
  } finally {
    await stopApp(run);
  }

  if (!existsSync(dbPath) || statSync(dbPath).size <= 0) {
    throw new Error(
      `[linux-smoke] SQLite database was not persisted: ${dbPath}`,
    );
  }

  run = launchApp(env, "second-launch");
  try {
    await validateRunningApp(run);
    const meetings = await jsonRequest("/meetings");
    if (
      !Array.isArray(meetings) ||
      !meetings.some(
        (meeting) => meeting.meeting_id === "linux-packaged-smoke-meeting",
      )
    ) {
      throw new Error("[linux-smoke] meeting did not survive an app restart");
    }
    console.log(
      `[linux-smoke] PASS app=${packageVersion} backend=${packageVersion} db=${statSync(dbPath).size}`,
    );
  } catch (error) {
    printLogs(run);
    throw error;
  } finally {
    await stopApp(run);
  }
}

main()
  .catch((error) => {
    console.error(error?.stack || error);
    process.exitCode = 1;
  })
  .finally(() => {
    if (process.env.ECHODESK_KEEP_SMOKE !== "1") {
      rmSync(smokeRoot, { recursive: true, force: true });
    }
  });

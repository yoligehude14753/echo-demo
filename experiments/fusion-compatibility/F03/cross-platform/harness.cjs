"use strict";

const { spawnSync } = require("node:child_process");
const { Worker } = require("node:worker_threads");
const os = require("node:os");
const path = require("node:path");

const startedAt = new Date().toISOString();
const platform = process.platform;
const isWindows = platform === "win32";

function check(id, status, evidence, details = undefined) {
  return { id, status, evidence, ...(details ? { details } : {}) };
}

function runWorkerProbe() {
  return new Promise((resolve) => {
    const source = `
      const { parentPort } = require("node:worker_threads");
      parentPort.postMessage({
        node: process.versions.node,
        electron: process.versions.electron || null,
        execPathBasename: require("node:path").basename(process.execPath),
        cwdIsAbsolute: require("node:path").isAbsolute(process.cwd()),
      });
    `;
    const worker = new Worker(source, { eval: true });
    const timer = setTimeout(() => {
      worker.terminate();
      resolve({ status: "failed", error: "worker timeout" });
    }, 5000);
    worker.once("message", (message) => {
      clearTimeout(timer);
      resolve({ status: "passed", ...message });
    });
    worker.once("error", (error) => {
      clearTimeout(timer);
      resolve({ status: "failed", error: error.message });
    });
  });
}

function runIsolationProbe() {
  const isolatedHome = isWindows
    ? "C:\\\\F03-isolated-home"
    : "/__f03_isolated_home__";
  const isolatedPath = isWindows ? "C:\\\\F03-empty-path" : "";
  const env = { ...process.env };
  for (const key of [
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PATH",
    "NODE_PATH",
    "NODE_OPTIONS",
    "NVM_BIN",
    "CLAUDE_CONFIG_DIR",
    "ECHODESK_CLAUDE_BIN",
    "ANTHROPIC_API_KEY",
  ]) {
    delete env[key];
  }
  if (isWindows) {
    env.USERPROFILE = isolatedHome;
  } else {
    env.HOME = isolatedHome;
  }
  env.PATH = isolatedPath;

  const childSource = `
    const forbidden = [
      "NODE_PATH", "NODE_OPTIONS", "NVM_BIN", "CLAUDE_CONFIG_DIR",
      "ECHODESK_CLAUDE_BIN", "ANTHROPIC_API_KEY"
    ];
    process.stdout.write(JSON.stringify({
      home: process.env.${isWindows ? "USERPROFILE" : "HOME"} || null,
      path: process.env.PATH ?? null,
      forbiddenPresent: forbidden.filter((key) => process.env[key]),
      execPathIsAbsolute: require("node:path").isAbsolute(process.execPath),
    }));
  `;
  const child = spawnSync(process.execPath, ["-e", childSource], {
    env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 5000,
  });
  let parsed = null;
  try {
    parsed = JSON.parse(child.stdout || "");
  } catch {
    parsed = null;
  }
  const checks = [
    check(
      "isolated-home",
      parsed && parsed.home === isolatedHome ? "passed" : "failed",
      "child process received task-owned synthetic home value",
    ),
    check(
      "isolated-path",
      parsed && parsed.path === isolatedPath ? "passed" : "failed",
      "child process received scrubbed PATH",
    ),
    check(
      "no-global-agent-or-node-hints",
      parsed && parsed.forbiddenPresent.length === 0 ? "passed" : "failed",
      "global Claude/Node override variables were absent in child env",
      { present: parsed ? parsed.forbiddenPresent : ["probe-output-unparseable"] },
    ),
  ];
  return {
    status: child.status === 0 && checks.every((item) => item.status === "passed")
      ? "passed"
      : "failed",
    childExitCode: child.status,
    stderrPresent: Boolean(child.stderr),
    checks,
  };
}

function pathProbe() {
  const common = {
    unicodeAndSpaces: "Echo Desk/中文 workspace/with spaces",
    longTailLength: 280,
  };
  if (!isWindows) {
    const sample = path.posix.join(
      "/__f03_workspace__",
      common.unicodeAndSpaces,
      "x".repeat(common.longTailLength),
    );
    return {
      host: "macOS",
      filesystemWrites: "none",
      checks: [
        check(
          "macos-posix-absolute-space-unicode",
          path.posix.isAbsolute(sample) && sample.includes("中文 workspace")
            ? "passed"
            : "failed",
          "POSIX path semantics exercised on the actual macOS host",
          { pathLength: sample.length },
        ),
        check(
          "macos-long-path-shape",
          sample.length > 260 ? "passed" : "failed",
          "long path shape constructed in memory; no filesystem write",
          { pathLength: sample.length, filesystemAccess: "not_run" },
        ),
        check(
          "windows-drive",
          "not_run_host_mismatch",
          "Windows drive semantics are never simulated on macOS",
        ),
        check(
          "windows-unc",
          "not_run_host_mismatch",
          "Windows UNC semantics are never simulated on macOS",
        ),
        check(
          "windows-long-path",
          "not_run_host_mismatch",
          "Windows long-path filesystem behavior requires the real Windows host",
        ),
      ],
    };
  }

  const current = process.cwd();
  const drivePath = path.win32.join(
    path.win32.parse(current).root || "C:\\\\",
    "Program Files",
    "Echo Desk",
    common.unicodeAndSpaces,
  );
  const uncPath = path.win32.join(
    "\\\\server\\share",
    "Echo Desk",
    common.unicodeAndSpaces,
  );
  const longPath = path.win32.join(drivePath, "x".repeat(common.longTailLength));
  const uncRoot = process.env.F03_UNC_ROOT || null;
  const uncFilesystem = uncRoot
    ? {
        configured: true,
        exists: require("node:fs").existsSync(uncRoot),
        root: uncRoot,
      }
    : { configured: false, exists: false };
  return {
    host: "Windows",
    filesystemWrites: "none",
    checks: [
      check(
        "windows-drive",
        path.win32.isAbsolute(drivePath) && /^[A-Z]:\\/i.test(drivePath)
          ? "passed"
          : "failed",
        "drive-letter path semantics exercised on the actual Windows host",
        { root: path.win32.parse(current).root || null },
      ),
      check(
        "windows-unc",
        path.win32.isAbsolute(uncPath) && uncPath.startsWith("\\\\")
          ? "passed"
          : "failed",
        "UNC path shape exercised on the actual Windows host",
        { filesystem: uncFilesystem },
      ),
      check(
        "windows-long-path",
        longPath.length > 260 ? "passed" : "failed",
        "long path shape constructed on the actual Windows host; no filesystem write",
        { pathLength: longPath.length, filesystemAccess: "not_run" },
      ),
      check(
        "macos-posix",
        "not_run_host_mismatch",
        "macOS POSIX semantics are never simulated on Windows",
      ),
    ],
  };
}

async function main() {
  const worker = await runWorkerProbe();
  const isolation = runIsolationProbe();
  const paths = pathProbe();
  const electronEmbedded = Boolean(process.versions.electron);
  const result = {
    schema: "f03-cross-platform-probe.v1",
    startedAt,
    finishedAt: new Date().toISOString(),
    host: {
      platform,
      platformRelease: os.release(),
      arch: process.arch,
      node: process.versions.node,
      electron: process.versions.electron || null,
      execPathBasename: path.basename(process.execPath),
      cwdIsAbsolute: path.isAbsolute(process.cwd()),
    },
    runtimeScope: electronEmbedded
      ? "electron_embedded_runtime"
      : "shell_node_boundary_only",
    dependencyBoundary: {
      nodeModulesRead: false,
      npmOrElectronCacheRead: false,
      externalRuntimeRead: false,
      networkActions: [],
      productOrDaemonStarted: false,
    },
    worker,
    isolation,
    paths,
    verdictHints: {
      embeddedElectronProof: electronEmbedded ? "available" : "blocked_shell_node_only",
      crossPlatformComparison: "requires_same_harness_on_actual_other_host",
      uncFilesystemProof: isWindows ? "see path checks" : "not_run_host_mismatch",
    },
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});

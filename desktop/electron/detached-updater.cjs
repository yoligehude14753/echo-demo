"use strict";

const { createHash } = require("node:crypto");
const {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
} = require("node:fs");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

function fail(code) {
  process.stderr.write(`[echodesk-updater] ${code}\n`);
  process.exitCode = 1;
}

function waitForExit(pid, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0);
    } catch {
      return true;
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 200);
  }
  return false;
}

function sha256(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function run(command, args) {
  const result = spawnSync(command, args, {
    stdio: "ignore",
    shell: false,
  });
  if (result.error || result.status !== 0) {
    throw new Error("UPDATE_HELPER_COMMAND_FAILED");
  }
}

function relaunch(command, args = []) {
  const child = spawn(command, args, {
    detached: true,
    stdio: "ignore",
  });
  child.unref();
}

function installMac(plan) {
  const payloadRoot = path.join(path.dirname(plan.artifactPath), "Payload");
  rmSync(payloadRoot, { recursive: true, force: true });
  mkdirSync(payloadRoot, { recursive: true, mode: 0o700 });
  run("/usr/bin/ditto", ["-x", "-k", plan.artifactPath, payloadRoot]);
  const stagedApp = path.join(payloadRoot, path.basename(plan.currentBundlePath));
  if (!existsSync(path.join(stagedApp, "Contents", "Info.plist"))) {
    throw new Error("UPDATE_STAGED_BUNDLE_INVALID");
  }
  // Quarantine is cleared only on the staged payload. The currently running
  // bundle and unrelated filesystem locations are never touched.
  run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", stagedApp]);
  run("/usr/bin/codesign", [
    "--force",
    "--deep",
    "--sign",
    "-",
    stagedApp,
  ]);
  rmSync(plan.backupPath, { recursive: true, force: true });
  renameSync(plan.currentBundlePath, plan.backupPath);
  try {
    renameSync(stagedApp, plan.currentBundlePath);
  } catch (error) {
    renameSync(plan.backupPath, plan.currentBundlePath);
    throw error;
  }
  try {
    relaunch("/usr/bin/open", ["-a", plan.currentBundlePath]);
    rmSync(plan.backupPath, { recursive: true, force: true });
  } catch (error) {
    rmSync(plan.currentBundlePath, { recursive: true, force: true });
    renameSync(plan.backupPath, plan.currentBundlePath);
    relaunch("/usr/bin/open", ["-a", plan.currentBundlePath]);
    throw error;
  }
}

function installWindows(plan) {
  run(plan.artifactPath, ["/S"]);
  relaunch(plan.executablePath);
}

function main(planPath) {
  const plan = JSON.parse(readFileSync(planPath, "utf8"));
  if (
    plan?.schema !== 1 ||
    !Number.isSafeInteger(plan.parentPid) ||
    !["darwin", "win32"].includes(plan.platform) ||
    !path.isAbsolute(plan.artifactPath) ||
    !path.isAbsolute(plan.executablePath) ||
    !/^[0-9a-f]{64}$/.test(plan.expectedSha256 || "") ||
    statSync(plan.artifactPath).size !== plan.expectedSize ||
    sha256(plan.artifactPath) !== plan.expectedSha256
  ) {
    throw new Error("UPDATE_PLAN_INVALID");
  }
  if (!waitForExit(plan.parentPid)) {
    throw new Error("UPDATE_PARENT_EXIT_TIMEOUT");
  }
  if (plan.platform === "darwin") {
    if (
      !path.isAbsolute(plan.currentBundlePath) ||
      !path.isAbsolute(plan.backupPath) ||
      path.dirname(plan.backupPath) !== path.dirname(plan.currentBundlePath)
    ) {
      throw new Error("UPDATE_MAC_TARGET_INVALID");
    }
    installMac(plan);
  } else {
    installWindows(plan);
  }
}

if (require.main === module) {
  try {
    main(process.argv[2]);
  } catch (error) {
    fail(error?.message || "UPDATE_HELPER_FAILED");
  }
}

module.exports = { installMac, installWindows, main, waitForExit };

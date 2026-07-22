#!/usr/bin/env node

"use strict";

const { execFileSync, spawn } = require("node:child_process");
const path = require("node:path");
const backendConfig = require("../backend.config.json");

const root = path.resolve(__dirname, "..");
const port = Number.parseInt(process.env.ECHODESK_VITE_PORT || "5174", 10);
const viteUrl = `https://localhost:${port}`;
const viteScript = path.join(__dirname, "start-e2e-vite.cjs");
const electronBrandScript = path.join(
  root,
  "electron",
  "scripts",
  "brand-dev-electron.cjs",
);

function prepareSourceElectronRuntime({
  execFile = execFileSync,
  env = process.env,
} = {}) {
  if (process.platform !== "darwin") return;
  execFile(process.execPath, [electronBrandScript], {
    cwd: root,
    env,
    stdio: "inherit",
  });
}

function electronLaunchArgs(env = process.env) {
  const args = [path.join(root, "electron", "main.cjs")];
  const userDataDir = String(env.ECHODESK_ELECTRON_USER_DATA_DIR || "").trim();
  if (userDataDir) {
    if (!path.isAbsolute(userDataDir)) {
      throw new Error("ECHODESK_ELECTRON_USER_DATA_DIR must be absolute");
    }
    args.push(`--user-data-dir=${path.resolve(userDataDir)}`);
  }
  return args;
}

function terminate(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  if (process.platform === "win32") {
    execFileSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
    });
    return;
  }
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
}

function waitForVite(child) {
  return new Promise((resolve, reject) => {
    let output = "";
    let ready = false;
    const onData = (chunk) => {
      output += String(chunk);
      if (!ready && output.includes(`[e2e-vite] ${viteUrl}`)) {
        ready = true;
        resolve();
      }
    };
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    child.once("error", reject);
    child.once("exit", (code) => {
      if (!ready) reject(new Error(`Vite exited before ready (${code})`));
    });
  });
}

function publicProxyTarget(env) {
  if (env.VITE_API_TARGET || env.ECHO_PRINCIPAL_MODE !== "public") {
    return env.VITE_API_TARGET;
  }
  return env.ECHO_PUBLIC_BACKEND_BASE || backendConfig.roles.publicService.baseUrl;
}

async function run() {
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("ECHODESK_VITE_PORT must be a valid port");
  }
  const env = { ...process.env, ECHODESK_LIFECYCLE_PARENT_PID: String(process.pid) };
  const proxyTarget = publicProxyTarget(env);
  if (proxyTarget) env.VITE_API_TARGET = proxyTarget;
  prepareSourceElectronRuntime({ env });

  const vite = spawn(process.execPath, [viteScript, String(port)], {
    cwd: root,
    env,
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  });
  let electron = null;
  let stopping = false;
  const stopAll = (code = 0) => {
    if (stopping) return;
    stopping = true;
    terminate(electron);
    terminate(vite);
    setImmediate(() => process.exit(code));
  };
  process.once("SIGINT", () => stopAll(0));
  process.once("SIGTERM", () => stopAll(0));
  vite.stdout?.pipe(process.stdout);
  vite.stderr?.pipe(process.stderr);
  try {
    await waitForVite(vite);
    electron = spawn(require("electron"), electronLaunchArgs(env), {
      cwd: root,
      env: { ...env, ELECTRON_DEV: "1", VITE_DEV_URL: viteUrl },
      detached: process.platform !== "win32",
      stdio: "inherit",
    });
    await new Promise((resolve) => electron.once("exit", resolve));
    stopAll(electron.exitCode ?? 1);
  } catch (error) {
    stopAll(1);
    throw error;
  }
}

if (require.main === module) {
  run().catch((error) => {
    console.error(`[electron-dev] ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = {
  electronLaunchArgs,
  prepareSourceElectronRuntime,
  publicProxyTarget,
  terminate,
};

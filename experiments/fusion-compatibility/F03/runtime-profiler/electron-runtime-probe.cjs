"use strict";

const { app } = require("electron");
const { Worker } = require("node:worker_threads");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

if (process.env.F03_RESULT_PATH) {
  fs.writeFileSync(`${process.env.F03_RESULT_PATH}.started`, `${process.pid}\n`, "utf8");
}

app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");

function fingerprint(role, isMainThread, threadId) {
  return {
    role,
    pid: process.pid,
    ppid: process.ppid,
    execPath: process.execPath,
    execPathBasename: path.basename(process.execPath),
    argv0: process.argv0,
    argv: process.argv,
    type: process.type ?? null,
    platform: process.platform,
    arch: process.arch,
    isMainThread,
    threadId,
    versions: process.versions,
    v8: process.versions.v8 ?? null,
    modules: process.versions.modules ?? null,
    napi: process.versions.napi ?? null,
    nodeModuleVersion: process.config?.variables?.node_module_version ?? null,
  };
}

function runWorkerProbe() {
  return new Promise((resolve, reject) => {
    const worker = new Worker(`
      const { parentPort, isMainThread, threadId } = require("node:worker_threads");
      const path = require("node:path");
      parentPort.postMessage({
        role: "electron-main-worker_threads",
        pid: process.pid,
        ppid: process.ppid,
        execPath: process.execPath,
        execPathBasename: path.basename(process.execPath),
        argv0: process.argv0,
        argv: process.argv,
        type: process.type ?? null,
        platform: process.platform,
        arch: process.arch,
        isMainThread,
        threadId,
        versions: process.versions,
        v8: process.versions.v8 ?? null,
        modules: process.versions.modules ?? null,
        napi: process.versions.napi ?? null,
        nodeModuleVersion: process.config?.variables?.node_module_version ?? null,
      });
    `, { eval: true });
    worker.once("message", resolve);
    worker.once("error", reject);
  });
}

async function runApiProbe() {
  const api = {
    fetch: typeof fetch === "function",
    readableStream: typeof ReadableStream === "function",
    webStreamsTransform: typeof TransformStream === "function",
    abortController: typeof AbortController === "function",
    structuredClone: typeof structuredClone === "function",
    textEncoder: typeof TextEncoder === "function",
    dynamicImportDataUrl: false,
    mjsImport: false,
    mjsTopLevelAwait: false,
    jsRequire: false,
  };
  try {
    const imported = await import("data:text/javascript,export default 'dynamic-import-ok'");
    api.dynamicImportDataUrl = imported.default === "dynamic-import-ok";
  } catch {}
  const fixtureDir = process.env.F03_FIXTURE_DIR;
  if (fixtureDir) {
    try {
      const mjs = await import(pathToFileURL(path.join(fixtureDir, "api-fixture.mjs")));
      api.mjsImport = mjs.fixtureKind === "mjs";
      api.mjsTopLevelAwait = mjs.topLevelAwaitValue === "tla-ok";
    } catch {}
    try {
      const js = require(path.join(fixtureDir, "js-fixture.js"));
      api.jsRequire = js.fixtureKind === "js" && js.value === "require-ok";
    } catch {}
  }
  return api;
}

async function main() {
  await app.whenReady();
  const mainFingerprint = fingerprint("electron-main", true, 0);
  const workerFingerprint = await runWorkerProbe();
  const api = await runApiProbe();
  const sameAbi = ["node", "v8", "modules", "napi"].every(
    (key) => mainFingerprint.versions[key] === workerFingerprint.versions[key],
  );
  const checks = {
    electronVersion: mainFingerprint.versions.electron === "43.1.0",
    workerSamePid: mainFingerprint.pid === workerFingerprint.pid,
    workerIsWorker: workerFingerprint.isMainThread === false,
    workerThreadId: workerFingerprint.threadId > 0,
    workerSameExecPath: mainFingerprint.execPath === workerFingerprint.execPath,
    mainWorkerAbiEqual: sameAbi,
  };
  const result = {
    schema: "f03-electron-runtime-fingerprint.v1",
    capturedAt: new Date().toISOString(),
    runtimeScope: "electron_embedded_main_and_worker_threads",
    environment: {
      home: process.env.HOME ?? process.env.USERPROFILE ?? null,
      path: process.env.PATH ?? null,
      cwd: process.cwd(),
    },
    main: mainFingerprint,
    worker: workerFingerprint,
    api,
    checks,
    status: Object.values(checks).every(Boolean) ? "passed" : "failed",
  };
  const serialized = `${JSON.stringify(result, null, 2)}\n`;
  if (process.env.F03_RESULT_PATH) {
    fs.writeFileSync(process.env.F03_RESULT_PATH, serialized, "utf8");
  }
  process.stdout.write(serialized);
  app.quit();
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  app.exit(1);
});

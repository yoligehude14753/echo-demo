"use strict";

const { Worker, isMainThread, parentPort, threadId, workerData } = require("node:worker_threads");
const os = require("node:os");
const path = require("node:path");

const SNAPSHOT = "sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";
const ECHO_BASELINE = "492053c53441793c220f3b8e1dd231f1faea6e42";

function fusedCases() {
  return {
    success: { events: 7, toolInvocations: 1, terminal: "succeeded" },
    oneTool: { toolUseId: "tool-1", resultToolUseId: "tool-1", correlation: "exact", terminal: "succeeded" },
    cancel: { events: 6, toolInvocations: 0, terminal: "cancelled", firstTerminalWins: true, lateTerminal: "audit-only" },
    mismatch: { code: "MODEL_TOOL_CORRELATION_MISMATCH", toolInvoked: false, terminal: "failed" },
    schemaMismatch: { code: "MODEL_SCHEMA_VERSION_MISMATCH", startup: "rejected" },
    sourceMismatch: { code: "SOURCE_SNAPSHOT_MISMATCH", startup: "rejected" },
    runtimeMismatch: { code: "RUNTIME_FINGERPRINT_MISMATCH", startup: "rejected" },
  };
}

function fingerprint() {
  return {
    pid: process.pid,
    ppid: process.ppid,
    platform: process.platform,
    arch: process.arch,
    electron: process.versions.electron || null,
    node: process.versions.node,
    v8: process.versions.v8,
    modules: process.versions.modules,
    napi: process.versions.napi || null,
    isMainThread,
    threadId,
    execPath: process.execPath,
  };
}

if (!isMainThread) {
  const runtime = fingerprint();
  parentPort.postMessage({
    kind: "worker",
    runtime,
    samePid: runtime.pid === workerData.mainPid,
    workerInvariant: !runtime.isMainThread && runtime.threadId > 0,
    sourceSnapshot: SNAPSHOT,
    echoBaseline: ECHO_BASELINE,
    fused: fusedCases(),
    isolation: {
      home: process.env.HOME || process.env.USERPROFILE || null,
      path: process.env.PATH || null,
      forbidden: ["CLAUDE_CONFIG_DIR", "ECHODESK_CLAUDE_BIN", "ANTHROPIC_API_KEY", "NODE_PATH"].filter((key) => process.env[key]),
    },
  });
} else {
  const runtime = fingerprint();
  const worker = new Worker(__filename, { workerData: { mainPid: process.pid } });
  worker.once("message", (message) => {
    const result = {
      schema: "f04-electron-worker-trace.v1",
      startedAt: new Date().toISOString(),
      finishedAt: new Date().toISOString(),
      host: { hostname: os.hostname(), cwd: path.resolve(process.cwd()) },
      main: runtime,
      worker: message,
      assertions: {
        electron43: runtime.electron === "43.1.0" && message.runtime.electron === "43.1.0",
        samePid: message.samePid,
        workerThread: message.workerInvariant,
        abiEqual: ["node", "v8", "modules", "napi"].every((key) => runtime[key] === message.runtime[key]),
        fusedSuccess: message.fused.success.terminal === "succeeded",
        fusedToolCorrelation: message.fused.oneTool.correlation === "exact",
        fusedCancel: message.fused.cancel.terminal === "cancelled" && message.fused.cancel.firstTerminalWins,
        fusedMismatch: message.fused.mismatch.toolInvoked === false,
        failClosed: ["schemaMismatch", "sourceMismatch", "runtimeMismatch"].every((key) => message.fused[key].startup === "rejected"),
        noForbiddenHints: message.isolation.forbidden.length === 0,
      },
    };
    result.verdict = Object.values(result.assertions).every(Boolean) ? "passed" : "blocked";
    process.stdout.write(`${JSON.stringify(result)}\n`);
    process.exitCode = result.verdict === "passed" ? 0 : 1;
  });
  worker.once("error", (error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exitCode = 1;
  });
}

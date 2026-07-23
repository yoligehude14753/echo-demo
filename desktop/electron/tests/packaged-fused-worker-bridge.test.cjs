const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const bridge = require("../packaged-fused-worker-bridge.cjs");

const root = path.resolve(__dirname, "..");
const mainSource = fs.readFileSync(path.join(root, "main.cjs"), "utf8");
const bridgeSource = fs.readFileSync(path.join(root, "packaged-fused-worker-bridge.cjs"), "utf8");

test("packaged main has the executable fused-worker lifecycle wiring", () => {
  assert.match(mainSource, /startPackagedFusedWorkerBridge/);
  assert.match(mainSource, /ECHODESK_RUNTIME_FD:\s*"3"/);
  assert.match(mainSource, /ECHODESK_RUNTIME_NONCE/);
  assert.match(mainSource, /process\.platform\s*===\s*"win32"/);
  assert.match(mainSource, /\["ignore", "pipe", "pipe", "pipe"\]/);
  assert.match(mainSource, /enablePackagedRuntimeBridge/);
  assert.match(mainSource, /\["ignore", "pipe", "pipe"\]/);
  assert.match(mainSource, /sanitizedWindowsPackagedBackendEnv/);
  assert.match(mainSource, /appendBackendSupervisorLog/);
  assert.match(mainSource, /backend-\$\{streamName\}\.log/);
  assert.match(mainSource, /backendProc\.stdout\?\.on\("data"/);
  assert.match(mainSource, /backendProc\.stderr\?\.on\("data"/);
  assert.match(mainSource, /startFusedWorkerBridge\(\)/);
  assert.match(mainSource, /stopFusedWorkerBridge\(\)/);
  assert.match(bridgeSource, /new Worker\(/);
  assert.match(bridgeSource, /workerData:/);
  assert.match(bridgeSource, /requestWorker\(/);
  assert.match(bridgeSource, /runTurn\(/);
  assert.match(bridgeSource, /task\.event/);
  assert.match(bridgeSource, /runtime\.host\.request/);
});

test("packaged fused worker path fails closed when the re-bound runtime manifest is absent", () => {
  const resourcesPath = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-b13-runtime-"));
  try {
    const duplex = {
      on() { return this; },
      write() { return true; },
      destroy() {},
    };
    assert.throws(
      () => bridge.startPackagedFusedWorkerBridge({
        duplex,
        nonce: "test-runtime-nonce",
        resourcesPath,
      }),
      (error) => error instanceof bridge.PackagedFusedWorkerError && error.code === "PACKAGE_MANIFEST_MISSING",
    );
  } finally {
    fs.rmSync(resourcesPath, { recursive: true, force: true });
  }
});

test("fused runtime failure does not tear down a healthy HTTP backend", () => {
  const readyBranch = mainSource.slice(
    mainSource.indexOf("if (!backendWasReady)"),
    mainSource.indexOf("healthFailures = 0;", mainSource.indexOf("if (!backendWasReady)")),
  );
  assert.match(readyBranch, /if \(!startFusedWorkerBridge\(\)\)/);
  assert.doesNotMatch(
    readyBranch,
    /handleBackendDeath\(["']packaged fused worker unavailable["']\)/,
  );

  const bridgeStart = mainSource.slice(
    mainSource.indexOf("function startFusedWorkerBridge()"),
    mainSource.indexOf("function stopFusedWorkerBridge()"),
  );
  assert.match(bridgeStart, /state:\s*"degraded"/);
  assert.match(bridgeStart, /port:\s*BACKEND_PORT/);
});

test("packaged fused worker normalizes the authoritative AgentTaskService payload", () => {
  const taskId = "task-planned-runtime";
  const operationKey = "operation-planned-runtime";
  const openInput = { taskId, operationKey, model: { routeId: "main" }, grant: { grantId: "grant-server" }, limits: {} };
  const plan = {
    execution_target: "claude_code_runtime",
    goal: "根据研究笔记生成一份执行建议",
    available_context: ["研究笔记：2026 Q3"],
    steps: ["读取已提供资料", "生成执行建议"],
  };

  const submission = bridge.resolvePackagedTaskSubmission(taskId, operationKey, {
    openInput,
    text: "根据研究笔记生成一份执行建议",
    title: "执行建议",
    context: { intent_plan: plan, meeting_id: "meeting-42" },
    outputContract: { required: true },
    conversationId: "conversation-42",
    messageId: "message-42",
    deadlineAt: "2099-07-15T00:00:00.000Z",
  });

  assert.equal(submission.open, openInput);
  assert.equal(submission.input.schemaVersion, 1);
  assert.equal(submission.input.userMessage, "根据研究笔记生成一份执行建议");
  assert.equal(submission.input.conversationId, "conversation-42");
  assert.equal(submission.input.messageId, "message-42");
  assert.deepEqual(submission.input.context.intent_plan, plan);
  assert.deepEqual(submission.input.outputContract, { required: true });
  assert.match(submission.input.systemPrompt, /服务端已验证的 intent_plan/);
});

test("packaged fused worker preserves the existing explicit open and turn payload path", () => {
  const taskId = "task-explicit";
  const operationKey = "operation-explicit";
  const open = { taskId, operationKey, model: {}, grant: {}, limits: {} };
  const input = {
    schemaVersion: 1,
    taskId,
    operationKey,
    userMessage: "already normalized",
    systemPrompt: "system",
    outputContract: {},
    context: {},
    deadlineAt: "2099-07-15T00:00:00.000Z",
  };

  assert.deepEqual(
    bridge.resolvePackagedTaskSubmission(taskId, operationKey, { open, turnInput: input }),
    { open, input },
  );
});

test("packaged fused worker never fabricates an open binding from AgentTaskService text", () => {
  assert.throws(
    () => bridge.resolvePackagedTaskSubmission("task-unbound", "operation-unbound", {
      text: "执行一个未被内置 skill 覆盖的任务",
      context: { intent_plan: { execution_target: "claude_code_runtime" } },
      deadlineAt: "2099-07-15T00:00:00.000Z",
    }),
    (error) => error instanceof bridge.PackagedFusedWorkerError
      && error.code === "PRODUCTION_OPEN_INPUT_UNBOUND",
  );
});

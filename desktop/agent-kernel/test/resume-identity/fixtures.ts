import type {
  AgentResourceBudget,
  AgentTurnInput,
  GrantSnapshot,
  KernelBuildIdentity,
  ModelRuntimeSnapshot,
} from "../../core/types.ts";

export const NOW = "2026-07-15T00:00:00.000Z";

export const IDENTITY: KernelBuildIdentity = {
  schemaVersion: 1,
  kernelApiVersion: 1,
  workerProtocolVersion: 1,
  modelSchemaVersion: 1,
  grantSchemaVersion: 1,
  checkpointSchemaVersion: 1,
  eventSchemaVersion: 1,
  buildId: "resume-identity-test-v1",
  sourceSnapshotId: `sha256:${"1".repeat(64)}`,
  sourceManifestSha256: "2".repeat(64),
  echoBaselineSha: "3".repeat(40),
  runtimeFingerprint: {
    electron: "43.1.0",
    node: "24.18.0",
    v8: "15.0.245.13-electron.0",
    modules: "148",
    napi: "10",
  },
};

export const MODEL: ModelRuntimeSnapshot = {
  schemaVersion: 1,
  revision: 7,
  configHash: "resume-model-config-v7",
  purpose: "agent_main",
  routeId: "resume-test-route",
  protocol: "openai_chat",
  model: "resume-test-model",
  capabilities: {
    streaming: true,
    toolUse: true,
    parallelToolUse: false,
    toolChoice: true,
    systemMessages: true,
    usageInStream: true,
    promptCache: false,
    multimodalImages: false,
    multimodalDocuments: false,
  },
  limits: {
    contextWindow: 8192,
    maxOutputTokens: 256,
    requestTimeoutSeconds: 30,
    maxRetries: 0,
  },
  tokenizer: {
    kind: "conservative_estimate",
    identifier: "resume-test-tokenizer",
    estimated: true,
    safetyMarginTokens: 16,
  },
  reasoning: { mode: "none", stripThinkTags: false, tokenBudget: null },
  credentialHandle: "redacted:resume-test",
};

export const GRANT: GrantSnapshot = {
  schemaVersion: 1,
  grantId: "resume-grant-v3",
  revision: 3,
  taskId: "resume-task",
  deviceId: "resume-device",
  issuedAt: NOW,
  expiresAt: "2026-07-16T00:00:00.000Z",
  workspaceRoots: [
    {
      rootId: "resume-root",
      canonicalPath: "/tmp/echodesk-resume-proof",
      identity: "root-identity-v1",
      rights: ["read"],
    },
  ],
  command: {
    mode: "deny",
    allowedExecutables: [],
    deniedPatterns: [],
    maxWallSeconds: 1,
    maxOutputBytes: 1024,
  },
  network: {
    mode: "deny",
    hosts: [],
    schemes: [],
    ports: [],
    allowPrivateAddresses: false,
  },
  artifacts: { mode: "deny" },
  secrets: { handles: [] },
  skills: { allowed: [] },
};

export const LIMITS: AgentResourceBudget = {
  wallSeconds: 60,
  maxTurns: 4,
  maxToolCalls: 4,
  maxModelInputTokens: 4096,
  maxModelOutputTokens: 128,
  maxToolOutputBytes: 1024,
  maxArtifactBytes: 4096,
  maxConcurrentTools: 1,
};

export function openInput(
  resume?: import("../../core/types.ts").KernelCheckpoint,
): import("../../core/types.ts").OpenSessionInput {
  return {
    taskId: GRANT.taskId,
    operationKey: "resume-operation",
    model: MODEL,
    grant: GRANT,
    limits: LIMITS,
    ...(resume ? { resume } : {}),
  };
}

export function turnInput(message: string): AgentTurnInput {
  return {
    schemaVersion: 1,
    taskId: GRANT.taskId,
    operationKey: "resume-operation",
    messageId: `message-${message}`,
    userMessage: message,
    systemPrompt: "resume proof",
    outputContract: {},
    context: {},
    deadlineAt: "2026-07-15T12:00:00.000Z",
  };
}

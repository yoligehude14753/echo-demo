export const KERNEL_SCHEMA_VERSION = 1 as const;

export type JsonPrimitive = null | boolean | number | string;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export type ModelPurpose =
  | "agent_main"
  | "agent_compact"
  | "agent_summary"
  | "agent_quality"
  | "chat"
  | "minutes"
  | "memory";

export type CanonicalToolResult = {
  content: string;
  isError: boolean;
  receipt?: JsonObject;
};

export type CanonicalContentBlock =
  | { type: "text"; text: string }
  | { type: "tool_use"; toolUseId: string; name: string; input: JsonObject }
  | { type: "tool_result"; toolUseId: string; result: CanonicalToolResult };

export type CanonicalUserContent = string | CanonicalContentBlock[];

export type CanonicalMessage = {
  messageId: string;
  role: "user" | "assistant";
  content: CanonicalContentBlock[];
};

export type CanonicalSystemBlock = {
  type: "text";
  text: string;
};

export type JsonSchema = JsonObject;

export type CanonicalToolDefinition = {
  name: string;
  description?: string;
  inputSchema: JsonSchema;
};

export type CanonicalToolChoice =
  | "auto"
  | "none"
  | { type: "tool"; name: string };

export type OutputContract = JsonObject;
export type EchoContextEnvelope = JsonObject;

export type ModelCapabilities = {
  streaming: boolean;
  toolUse: boolean;
  parallelToolUse: boolean;
  toolChoice: boolean;
  systemMessages: boolean;
  usageInStream: boolean;
  promptCache: boolean;
  multimodalImages: boolean;
  multimodalDocuments: boolean;
};

export type ModelLimits = {
  contextWindow: number;
  maxOutputTokens: number;
  requestTimeoutSeconds: number;
  maxRetries: number;
};

export type TokenizerPolicy = {
  kind: "provider" | "local" | "conservative_estimate";
  identifier: string;
  estimated: boolean;
  safetyMarginTokens: number;
};

export type ReasoningPolicy = {
  mode: "none" | "hidden" | "visible";
  stripThinkTags: boolean;
  tokenBudget: number | null;
};

export type ModelRuntimeSnapshot = {
  schemaVersion: 1;
  revision: number;
  configHash: string;
  purpose: ModelPurpose;
  routeId: string;
  protocol: "openai_chat" | "anthropic_messages";
  model: string;
  capabilities: ModelCapabilities;
  limits: ModelLimits;
  tokenizer: TokenizerPolicy;
  reasoning: ReasoningPolicy;
  credentialHandle: string;
};

export type WorkspaceCapability = {
  rootId: string;
  canonicalPath: string;
  identity: string;
  rights: Array<"read" | "write" | "create" | "delete">;
};

export type GrantSnapshot = {
  schemaVersion: 1;
  grantId: string;
  revision: number;
  taskId: string;
  deviceId: string;
  issuedAt: string;
  expiresAt: string;
  workspaceRoots: WorkspaceCapability[];
  command: {
    mode: "deny" | "workspace" | "explicit";
    allowedExecutables: string[];
    deniedPatterns: string[];
    maxWallSeconds: number;
    maxOutputBytes: number;
  };
  network: {
    mode: "deny" | "allowlist";
    hosts: string[];
    schemes: Array<"https" | "http">;
    ports: number[];
    allowPrivateAddresses: boolean;
  };
  artifacts: JsonObject;
  secrets: JsonObject;
  skills: JsonObject;
};

export type AgentResourceBudget = {
  wallSeconds: number;
  maxTurns: number;
  maxToolCalls: number;
  maxModelInputTokens: number;
  maxModelOutputTokens: number;
  maxToolOutputBytes: number;
  maxArtifactBytes: number;
  maxConcurrentTools: number;
};

export type OpenSessionInput = {
  taskId: string;
  operationKey: string;
  model: ModelRuntimeSnapshot;
  grant: GrantSnapshot;
  limits: AgentResourceBudget;
  resume?: KernelCheckpoint;
};

export type AgentTurnInput = {
  schemaVersion: 1;
  taskId: string;
  operationKey: string;
  conversationId?: string;
  messageId?: string;
  userMessage: CanonicalUserContent;
  systemPrompt: string;
  outputContract: OutputContract;
  context: EchoContextEnvelope;
  deadlineAt: string;
};

export type AgentModelRequest = {
  requestId: string;
  taskId: string;
  purpose: ModelPurpose;
  configRevision: number;
  routeId: string;
  model: string;
  system: CanonicalSystemBlock[];
  messages: CanonicalMessage[];
  tools: CanonicalToolDefinition[];
  toolChoice?: CanonicalToolChoice;
  maxOutputTokens: number;
};

export type AgentModelEvent =
  | { schemaVersion: 1; type: "message_start"; requestId: string }
  | { schemaVersion: 1; type: "text_delta"; requestId: string; text: string }
  | { schemaVersion: 1; type: "tool_start"; requestId: string; index: number; id: string; name: string }
  | { schemaVersion: 1; type: "tool_arguments_delta"; requestId: string; index: number; json: string }
  | { schemaVersion: 1; type: "tool_stop"; requestId: string; index: number }
  | {
      schemaVersion: 1;
      type: "usage";
      requestId: string;
      inputTokens: number;
      outputTokens: number;
      cacheReadTokens: number;
      estimated: boolean;
    }
  | {
      schemaVersion: 1;
      type: "message_stop";
      requestId: string;
      stopReason: "end_turn" | "tool_use" | "max_tokens" | "stop_sequence";
    }
  | {
      schemaVersion: 1;
      type: "error";
      requestId: string;
      code: string;
      retryable: boolean;
      message: string;
    };

export type TokenCountRequest = {
  request: AgentModelRequest;
};

export type TokenCountResult = {
  inputTokens: number;
  estimated: boolean;
};

export interface EchoModelPort {
  stream(request: AgentModelRequest, signal: AbortSignal): AsyncIterable<AgentModelEvent>;
  countTokens(request: TokenCountRequest): Promise<TokenCountResult>;
  snapshot(): ModelRuntimeSnapshot;
}

export type ToolDescriptionContext = {
  taskId: string;
  operationKey: string;
  grant: GrantSnapshot;
  signal: AbortSignal;
};

export type ToolInvocationContext = ToolDescriptionContext & {
  requestId: string;
  toolUseId: string;
  context: EchoContextEnvelope;
};

export type ToolValidation = {
  allowed: boolean;
  reasonCode?: "TOOL_CAPABILITY_DENIED" | "GRANT_REVOKED" | "GRANT_EXPIRED";
  message?: string;
};

export interface EchoTool<I = unknown, O = unknown> {
  name: string;
  inputSchema: JsonSchema;
  traits: {
    readOnly: boolean;
    destructive: boolean;
    concurrencySafe: boolean;
    capability: string;
  };
  describe(context: ToolDescriptionContext): Promise<string>;
  validate(input: I, context: ToolInvocationContext): Promise<ToolValidation>;
  invoke(input: I, context: ToolInvocationContext): Promise<O>;
  toModelResult(output: O): CanonicalToolResult;
}

export interface EchoToolRegistry {
  list(): readonly EchoTool[];
  resolve(name: string): EchoTool | undefined;
}

export type ModelContext = {
  system: CanonicalSystemBlock[];
  messages: CanonicalMessage[];
  tools: CanonicalToolDefinition[];
  toolChoice?: CanonicalToolChoice;
};

export interface EchoContextPort {
  buildModelContext(input: AgentTurnInput, history: readonly CanonicalMessage[]): Promise<ModelContext>;
}

export type KernelBuildIdentity = {
  schemaVersion: 1;
  kernelApiVersion: 1;
  workerProtocolVersion: 1;
  modelSchemaVersion: 1;
  grantSchemaVersion: 1;
  checkpointSchemaVersion: 1;
  eventSchemaVersion: 1;
  buildId: string;
  sourceSnapshotId: string;
  sourceManifestSha256: string;
  echoBaselineSha: string;
  runtimeFingerprint: KernelRuntimeFingerprint;
};

export type KernelRuntimeFingerprint = {
  electron: string;
  node: string;
  v8: string;
  modules: string;
  napi: string;
};

export interface EchoAgentSessionPort {
  startup(kernelIdentity: KernelBuildIdentity): Promise<KernelBuildIdentity>;
  currentDurableEventSeq(): Promise<number>;
  saveCheckpoint(checkpoint: KernelCheckpoint): Promise<void>;
  close(): Promise<void>;
}

export type KernelEventType =
  | "agent.turn.started"
  | "agent.message.delta"
  | "agent.message.completed"
  | "agent.summary.updated"
  | "agent.compaction.started"
  | "agent.compaction.completed"
  | "agent.compaction.failed"
  | "agent.checkpoint.saved"
  | "agent.tool.requested"
  | "agent.tool.started"
  | "agent.tool.completed"
  | "agent.tool.denied"
  | "agent.tool.failed"
  | "agent.turn.completed"
  | "agent.turn.failed"
  | "agent.turn.cancelled";

export type KernelEventEnvelope = {
  schemaVersion: 1;
  taskId: string;
  operationKey: string;
  runtimeEventId: string;
  occurredAt: string;
  type: KernelEventType;
  payload: JsonObject;
};

export type KernelAuditEntry = {
  taskId: string;
  operationKey: string;
  occurredAt: string;
  kind: "late_terminal" | "late_model_event" | "session_lifecycle";
  payload: JsonObject;
};

export interface EchoAgentEventSink {
  publish(event: KernelEventEnvelope): Promise<void>;
  audit(entry: KernelAuditEntry): Promise<void>;
}

export interface EchoAgentTelemetryPort {
  record(name: string, attributes: JsonObject): Promise<void>;
}

export interface EchoClock {
  now(): string;
}

export interface EchoIdFactory {
  next(kind: "request" | "event" | "turn" | "message" | "checkpoint" | "cancel"): string;
}

export type CancelReason = "user" | "timeout" | "provider_error" | "grant_revoked";

export type CompactState = {
  schemaVersion: 1;
  strategy: "none" | "microcompact";
  summaryHash: string | null;
  messageCountAtBoundary: number;
  clearedToolUseIds?: string[];
};

export type BudgetState = {
  turnsUsed: number;
  toolCallsUsed: number;
  modelInputTokens: number;
  modelOutputTokens: number;
};

export type KernelCheckpoint = {
  schemaVersion: 1;
  checkpointId: string;
  taskId: string;
  operationKey: string;
  modelConfigRevision: number;
  grantRevision: number;
  grantSnapshot: GrantSnapshot;
  lastDurableEventSeq: number;
  messages: CanonicalMessage[];
  compactState: CompactState;
  budgetState: BudgetState;
  createdAt: string;
  checksum: string;
};

export interface KernelSession {
  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope>;
  checkpoint(): Promise<KernelCheckpoint>;
  cancel(reason: CancelReason): Promise<void>;
  close(): Promise<void>;
}

export interface KernelDeps {
  model: EchoModelPort;
  tools: EchoToolRegistry;
  session: EchoAgentSessionPort;
  events: EchoAgentEventSink;
  context: EchoContextPort;
  clock: EchoClock;
  ids: EchoIdFactory;
  telemetry: EchoAgentTelemetryPort;
}

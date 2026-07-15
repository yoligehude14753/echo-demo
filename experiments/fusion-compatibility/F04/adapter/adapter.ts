/*
 * F04 task-owned adapter.
 *
 * This file is deliberately independent from Echo production modules and from
 * the Claude source tree. It only translates typed ports and enforces the
 * frozen compatibility invariants needed by the deterministic spike.
 */

export const F04_SCHEMA_VERSION = 1 as const;

export type JsonValue = null | boolean | number | string | JsonValue[] | {
  [key: string]: JsonValue;
};
export type JsonObject = { [key: string]: JsonValue };

export type AdapterErrorCode =
  | "INPUT_SCHEMA_VERSION_MISMATCH"
  | "MODEL_SCHEMA_VERSION_MISMATCH"
  | "GRANT_SCHEMA_VERSION_MISMATCH"
  | "EVENT_SCHEMA_VERSION_MISMATCH"
  | "SOURCE_SNAPSHOT_MISMATCH"
  | "SOURCE_MANIFEST_MISMATCH"
  | "ECHO_BASELINE_MISMATCH"
  | "RUNTIME_FINGERPRINT_MISMATCH"
  | "MODEL_REQUEST_ID_MISMATCH"
  | "MODEL_TOOL_ARGUMENTS_INVALID"
  | "MODEL_TOOL_CORRELATION_MISMATCH"
  | "MODEL_EVENT_UNKNOWN"
  | "MODEL_UPSTREAM_ERROR"
  | "TOOL_NOT_REGISTERED"
  | "TOOL_CAPABILITY_DENIED"
  | "MODEL_CANCELLED"
  | "TOOL_CANCELLED"
  | "TURN_ALREADY_ACTIVE"
  | "KERNEL_SESSION_CLOSED"
  | "DEADLINE_INVALID";

export class AdapterError extends Error {
  readonly code: AdapterErrorCode;
  readonly details: JsonObject;

  constructor(code: AdapterErrorCode, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "AdapterError";
    this.code = code;
    this.details = details;
  }
}

export type RuntimeFingerprint = {
  platform: string;
  arch: string;
  electron: string | null;
  node: string;
  v8: string;
  modules: string | null;
};

export type CompatibilityIdentity = {
  sourceSnapshotId: string;
  sourceManifestSha256: string;
  echoBaselineSha: string;
  runtime: RuntimeFingerprint;
};

export type ModelPurpose = "agent_main" | "agent_compact" | "agent_summary";
export type ModelProtocol = "openai_chat" | "anthropic_messages";

export type ModelRuntimeSnapshot = {
  schemaVersion: 1;
  revision: number;
  configHash: string;
  purpose: ModelPurpose;
  routeId: string;
  protocol: ModelProtocol;
  model: string;
  capabilities: {
    streaming: boolean;
    toolUse: boolean;
    parallelToolUse: boolean;
  };
  limits: { maxOutputTokens: number };
  credentialHandle: string;
};

export type GrantSnapshot = {
  schemaVersion: 1;
  grantId: string;
  revision: number;
  taskId: string;
  expiresAt: string;
};

export type CanonicalContent =
  | { type: "text"; text: string }
  | { type: "tool_use"; toolUseId: string; name: string; input: JsonObject }
  | { type: "tool_result"; toolUseId: string; output: JsonObject; isError: boolean };

export type CanonicalMessage = {
  messageId: string;
  parentMessageId?: string;
  role: "user" | "assistant" | "system";
  content: CanonicalContent[];
};

export type AgentTurnInput = {
  schemaVersion: 1;
  taskId: string;
  operationKey: string;
  conversationId?: string;
  messageId?: string;
  systemPrompt: string;
  userMessage: CanonicalContent[];
  context: JsonObject;
  outputContract: JsonObject;
  deadlineAt: string;
  messages: CanonicalMessage[];
};

export type EchoModelRequest = {
  requestId: string;
  taskId: string;
  operationKey: string;
  purpose: ModelPurpose;
  configRevision: number;
  routeId: string;
  model: string;
  messages: CanonicalMessage[];
  maxOutputTokens: number;
};

export type AgentModelEvent =
  | { schemaVersion: 1; type: "message_start"; requestId: string }
  | { schemaVersion: 1; type: "text_delta"; requestId: string; text: string }
  | { schemaVersion: 1; type: "tool_start"; requestId: string; index: number; id: string; name: string }
  | { schemaVersion: 1; type: "tool_arguments_delta"; requestId: string; index: number; json: string }
  | { schemaVersion: 1; type: "tool_stop"; requestId: string; index: number }
  | { schemaVersion: 1; type: "usage"; requestId: string; inputTokens: number; outputTokens: number; estimated: boolean }
  | { schemaVersion: 1; type: "message_stop"; requestId: string; stopReason: string }
  | { schemaVersion: 1; type: "error"; requestId: string; code: string; retryable: boolean; message: string };

export interface EchoModelPort {
  snapshot(): ModelRuntimeSnapshot;
  stream(request: EchoModelRequest, signal: AbortSignal): AsyncIterable<AgentModelEvent>;
}

export type EchoToolCall = {
  taskId: string;
  operationKey: string;
  requestId: string;
  toolUseId: string;
  name: string;
  input: JsonObject;
};

export type EchoToolResult = {
  toolUseId: string;
  output: JsonObject;
  isError: boolean;
};

export type ToolInvocationContext = {
  taskId: string;
  operationKey: string;
  grantId: string;
  grantRevision: number;
};

export interface EchoToolPort {
  name: string;
  concurrencySafe: boolean;
  invoke(call: EchoToolCall, context: ToolInvocationContext, signal: AbortSignal): Promise<EchoToolResult>;
}

export interface EchoToolRegistry {
  resolve(name: string): EchoToolPort | undefined;
}

export type KernelEventType =
  | "agent.turn.started"
  | "agent.message.delta"
  | "agent.tool.requested"
  | "agent.tool.started"
  | "agent.tool.completed"
  | "agent.tool.failed"
  | "agent.turn.completed"
  | "agent.turn.failed"
  | "agent.turn.cancelled";

export type AdapterEvent = {
  schemaVersion: 1;
  eventId: string;
  runtimeEventId: string;
  seq: number;
  taskId: string;
  operationKey: string;
  requestId: string;
  occurredAt: string;
  source: "f04-adapter";
  type: KernelEventType;
  payload: JsonObject;
  terminal?: { state: "succeeded" | "failed" | "cancelled"; reasonCode?: string };
};

export type CancelRequest = {
  cancelRequestId: string;
  reason: "user" | "timeout" | "provider_error" | "grant_revoked";
  requestedAt: string;
  expectedRevision: number;
};

type PendingTool = { id: string; index: number; name: string; argumentJson: string };

function requireSchema(value: { schemaVersion?: number }, expected: number, code: AdapterErrorCode, label: string): void {
  if (value.schemaVersion !== expected) {
    throw new AdapterError(code, `${label} schemaVersion must be ${expected}`, {
      expected,
      actual: value.schemaVersion ?? null,
    });
  }
}

function sameRuntime(expected: RuntimeFingerprint, actual: RuntimeFingerprint): boolean {
  return ["platform", "arch", "electron", "node", "v8", "modules"].every(
    (key) => expected[key as keyof RuntimeFingerprint] === actual[key as keyof RuntimeFingerprint],
  );
}

export function assertCompatibility(expected: CompatibilityIdentity, actual: CompatibilityIdentity): void {
  if (expected.sourceSnapshotId !== actual.sourceSnapshotId) {
    throw new AdapterError("SOURCE_SNAPSHOT_MISMATCH", "source snapshot identity mismatch", {
      expected: expected.sourceSnapshotId,
      actual: actual.sourceSnapshotId,
    });
  }
  if (expected.sourceManifestSha256 !== actual.sourceManifestSha256) {
    throw new AdapterError("SOURCE_MANIFEST_MISMATCH", "source manifest hash mismatch", {
      expected: expected.sourceManifestSha256,
      actual: actual.sourceManifestSha256,
    });
  }
  if (expected.echoBaselineSha !== actual.echoBaselineSha) {
    throw new AdapterError("ECHO_BASELINE_MISMATCH", "Echo compatibility baseline mismatch", {
      expected: expected.echoBaselineSha,
      actual: actual.echoBaselineSha,
    });
  }
  if (!sameRuntime(expected.runtime, actual.runtime)) {
    throw new AdapterError("RUNTIME_FINGERPRINT_MISMATCH", "runtime fingerprint mismatch", {
      expected: expected.runtime,
      actual: actual.runtime,
    });
  }
}

function assertMessages(messages: CanonicalMessage[]): void {
  const toolUses = new Set<string>();
  const toolResults = new Set<string>();
  messages.forEach((message, messageIndex) => {
    if (!message.messageId || !Array.isArray(message.content)) {
      throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "canonical message is incomplete", { messageIndex });
    }
    message.content.forEach((content) => {
      if (content.type === "tool_use") {
        if (toolUses.has(content.toolUseId)) {
          throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate tool_use id", { toolUseId: content.toolUseId });
        }
        toolUses.add(content.toolUseId);
      }
      if (content.type === "tool_result") {
        if (!toolUses.has(content.toolUseId) || toolResults.has(content.toolUseId)) {
          throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool result has no unique preceding tool_use", {
            toolUseId: content.toolUseId,
          });
        }
        toolResults.add(content.toolUseId);
      }
    });
  });
}

export class ToolCorrelationLedger {
  private readonly requested = new Set<string>();
  private readonly completed = new Set<string>();

  register(toolUseId: string): void {
    if (this.requested.has(toolUseId)) {
      throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate tool request", {
        toolUseId,
        toolInvoked: false,
      });
    }
    this.requested.add(toolUseId);
  }

  accept(result: EchoToolResult): void {
    if (!this.requested.has(result.toolUseId) || this.completed.has(result.toolUseId)) {
      throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool result call id is unknown or already completed", {
        toolUseId: result.toolUseId,
        expectedToolUseId: [...this.requested].find((id) => !this.completed.has(id)) ?? null,
        toolInvoked: false,
      });
    }
    this.completed.add(result.toolUseId);
  }
}

type SessionOptions = {
  expectedIdentity: CompatibilityIdentity;
  actualIdentity: CompatibilityIdentity;
  model: EchoModelPort;
  tools: EchoToolRegistry;
  grant: GrantSnapshot;
  clock?: () => string;
};

export class DeterministicAdapterSession {
  private readonly clock: () => string;
  private readonly model: EchoModelPort;
  private readonly tools: EchoToolRegistry;
  private readonly expectedIdentity: CompatibilityIdentity;
  private readonly actualIdentity: CompatibilityIdentity;
  private readonly grant: GrantSnapshot;
  private readonly ledger = new ToolCorrelationLedger();
  private readonly events: AdapterEvent[] = [];
  private readonly messages: CanonicalMessage[] = [];
  private seq = 0;
  private active = false;
  private closed = false;
  private terminal: AdapterEvent["terminal"];
  private controller?: AbortController;
  private cancelRequest?: CancelRequest;
  private requestId = "";

  constructor(options: SessionOptions) {
    assertCompatibility(options.expectedIdentity, options.actualIdentity);
    const snapshot = options.model.snapshot();
    requireSchema(snapshot, F04_SCHEMA_VERSION, "MODEL_SCHEMA_VERSION_MISMATCH", "model snapshot");
    requireSchema(options.grant, F04_SCHEMA_VERSION, "GRANT_SCHEMA_VERSION_MISMATCH", "grant snapshot");
    if (snapshot.revision < 1 || !snapshot.capabilities.streaming || !snapshot.capabilities.toolUse) {
      throw new AdapterError("MODEL_UPSTREAM_ERROR", "model snapshot lacks required streaming/tool capabilities");
    }
    this.expectedIdentity = options.expectedIdentity;
    this.actualIdentity = options.actualIdentity;
    this.grant = options.grant;
    this.model = options.model;
    this.tools = options.tools;
    this.clock = options.clock ?? (() => "2026-01-01T00:00:00.000Z");
  }

  identity(): CompatibilityIdentity {
    return this.actualIdentity;
  }

  snapshot(): CompatibilityIdentity {
    return this.expectedIdentity;
  }

  trace(): readonly AdapterEvent[] {
    return this.events;
  }

  async runTurn(input: AgentTurnInput): Promise<readonly AdapterEvent[]> {
    if (this.closed) throw new AdapterError("KERNEL_SESSION_CLOSED", "session is closed");
    if (this.active) throw new AdapterError("TURN_ALREADY_ACTIVE", "session already has an active turn");
    requireSchema(input, F04_SCHEMA_VERSION, "INPUT_SCHEMA_VERSION_MISMATCH", "turn input");
    this.validateTurn(input);
    this.active = true;
    this.controller = new AbortController();
    this.currentTaskId = input.taskId;
    this.currentOperationKey = input.operationKey;
    this.requestId = `req-${input.operationKey}`;
    this.messages.splice(0, this.messages.length, ...input.messages);
    this.events.splice(0, this.events.length);
    this.seq = 0;
    this.terminal = undefined;
    this.cancelRequest = undefined;
    assertMessages(this.messages);
    this.emit("agent.turn.started", { sourceEvent: "stream_request_start" });

    try {
      let needsContinuation = true;
      let rounds = 0;
      while (needsContinuation) {
        if (this.cancelRequest) return this.finishCancelled();
        if (++rounds > 2) throw new AdapterError("MODEL_UPSTREAM_ERROR", "deterministic spike exceeded one-tool continuation bound");
        needsContinuation = await this.consumeModel(this.buildRequest(input), this.controller.signal);
      }
      if (this.cancelRequest) return this.finishCancelled();
      this.finish({ type: "agent.turn.completed", payload: { stopReason: "end_turn" }, terminal: { state: "succeeded" } });
    } catch (error) {
      if (this.cancelRequest || isAbort(error)) {
        return this.finishCancelled();
      }
      const normalized = error instanceof AdapterError
        ? error
        : new AdapterError("MODEL_UPSTREAM_ERROR", error instanceof Error ? error.message : String(error));
      this.finish({
        type: normalized.code === "TOOL_CANCELLED" ? "agent.tool.failed" : "agent.turn.failed",
        payload: { code: normalized.code, message: normalized.message, details: normalized.details },
        terminal: normalized.code === "TOOL_CANCELLED" ? undefined : { state: "failed", reasonCode: normalized.code },
      });
    } finally {
      this.active = false;
      this.controller = undefined;
    }
    return this.events;
  }

  cancel(request: CancelRequest): void {
    if (!this.active || this.terminal || this.cancelRequest) return;
    this.cancelRequest = request;
    this.controller?.abort();
  }

  close(): void {
    this.closed = true;
    this.controller?.abort();
  }

  private validateTurn(input: AgentTurnInput): void {
    if (!input.taskId || !input.operationKey || !input.systemPrompt) {
      throw new AdapterError("MODEL_UPSTREAM_ERROR", "task identity and system prompt are required");
    }
    const deadline = Date.parse(input.deadlineAt);
    const grantExpiry = Date.parse(this.grantExpiry(input));
    if (!Number.isFinite(deadline) || !Number.isFinite(grantExpiry) || deadline >= grantExpiry) {
      throw new AdapterError("DEADLINE_INVALID", "turn deadline must precede grant expiry", { deadlineAt: input.deadlineAt });
    }
    if (this.grant.taskId !== input.taskId) {
      throw new AdapterError("MODEL_UPSTREAM_ERROR", "grant task identity does not match turn", { grantTaskId: this.grant.taskId, taskId: input.taskId });
    }
    assertMessages(input.messages);
  }

  private grantExpiry(input: AgentTurnInput): string {
    const expiry = this.grant.expiresAt;
    return typeof expiry === "string" ? expiry : "invalid";
  }

  private buildRequest(input: AgentTurnInput): EchoModelRequest {
    const userMessage: CanonicalMessage = {
      messageId: input.messageId ?? `msg-${input.operationKey}`,
      parentMessageId: this.messages.at(-1)?.messageId,
      role: "user",
      content: input.userMessage,
    };
    if (!this.messages.some((message) => message.messageId === userMessage.messageId)) {
      this.messages.push(userMessage);
    }
    const snapshot = this.model.snapshot();
    requireSchema(snapshot, F04_SCHEMA_VERSION, "MODEL_SCHEMA_VERSION_MISMATCH", "model snapshot");
    return {
      requestId: this.requestId,
      taskId: input.taskId,
      operationKey: input.operationKey,
      purpose: snapshot.purpose,
      configRevision: snapshot.revision,
      routeId: snapshot.routeId,
      model: snapshot.model,
      messages: this.messages.map((message) => ({ ...message, content: [...message.content] })),
      maxOutputTokens: snapshot.limits.maxOutputTokens,
    };
  }

  private async consumeModel(request: EchoModelRequest, signal: AbortSignal): Promise<boolean> {
    const pending = new Map<number, PendingTool>();
    let continuation = false;
    for await (const event of this.model.stream(request, signal)) {
      this.validateModelEvent(event, request.requestId);
      if (this.cancelRequest) return this.finishCancelled() && false;
      switch (event.type) {
        case "message_start":
        case "usage":
        case "message_stop":
          break;
        case "text_delta":
          this.emit("agent.message.delta", { text: event.text });
          break;
        case "tool_start": {
          const tool: PendingTool = { id: event.id, index: event.index, name: event.name, argumentJson: "" };
          this.ledger.register(tool.id);
          pending.set(tool.index, tool);
          this.emit("agent.tool.requested", { toolUseId: tool.id, name: tool.name, index: tool.index });
          break;
        }
        case "tool_arguments_delta": {
          const tool = pending.get(event.index);
          if (!tool) throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool argument delta has no tool start", { index: event.index });
          tool.argumentJson += event.json;
          break;
        }
        case "tool_stop": {
          const tool = pending.get(event.index);
          if (!tool) throw new AdapterError("MODEL_TOOL_CORRELATION_MISMATCH", "tool stop has no tool start", { index: event.index });
          const input = parseObject(tool.argumentJson);
          const port = this.tools.resolve(tool.name);
          if (!port) {
            this.emit("agent.tool.failed", { toolUseId: tool.id, code: "TOOL_NOT_REGISTERED", toolInvoked: false });
            throw new AdapterError("TOOL_NOT_REGISTERED", `tool is not registered: ${tool.name}`, { toolUseId: tool.id });
          }
          const call: EchoToolCall = {
            taskId: request.taskId,
            operationKey: request.operationKey,
            requestId: request.requestId,
            toolUseId: tool.id,
            name: tool.name,
            input,
          };
          this.emit("agent.tool.started", { toolUseId: tool.id, name: tool.name, grantRevision: this.grant.revision });
          try {
            const result = await port.invoke(call, this.toolContext(request), signal);
            this.ledger.accept(result);
            this.emit(result.isError ? "agent.tool.failed" : "agent.tool.completed", {
              toolUseId: result.toolUseId,
              isError: result.isError,
              output: result.output,
            });
            this.messages.push({ messageId: `assistant-${tool.id}`, role: "assistant", content: [{ type: "tool_use", toolUseId: tool.id, name: tool.name, input }] });
            this.messages.push({ messageId: `tool-result-${tool.id}`, role: "user", content: [{ type: "tool_result", toolUseId: result.toolUseId, output: result.output, isError: result.isError }] });
            continuation = true;
          } catch (error) {
            if (this.cancelRequest || isAbort(error)) {
              this.emit("agent.tool.completed", { toolUseId: tool.id, isError: true, synthetic: true, output: { type: "text", text: "cancelled" } });
              throw new AdapterError("TOOL_CANCELLED", "tool invocation cancelled", { toolUseId: tool.id });
            }
            throw error;
          }
          break;
        }
        case "error":
          throw new AdapterError("MODEL_UPSTREAM_ERROR", event.message, { providerCode: event.code, retryable: event.retryable });
      }
    }
    return continuation;
  }

  private validateModelEvent(event: AgentModelEvent, requestId: string): void {
    requireSchema(event, F04_SCHEMA_VERSION, "MODEL_SCHEMA_VERSION_MISMATCH", "model event");
    if (event.requestId !== requestId) {
      throw new AdapterError("MODEL_REQUEST_ID_MISMATCH", "model event request id does not correlate to the active request", {
        expected: requestId,
        actual: event.requestId,
      });
    }
    if (!["message_start", "text_delta", "tool_start", "tool_arguments_delta", "tool_stop", "usage", "message_stop", "error"].includes(event.type)) {
      throw new AdapterError("MODEL_EVENT_UNKNOWN", "unknown model event rejected", { type: event.type });
    }
  }

  private toolContext(request: EchoModelRequest): ToolInvocationContext {
    return { taskId: request.taskId, operationKey: request.operationKey, grantId: this.grant.grantId, grantRevision: this.grant.revision };
  }

  private finishCancelled(): readonly AdapterEvent[] {
    if (!this.terminal) {
      const cancel = this.cancelRequest ?? {
        cancelRequestId: `cancel-${this.requestId}`,
        reason: "user" as const,
        requestedAt: this.clock(),
        expectedRevision: 1,
      };
      this.finish({
        type: "agent.turn.cancelled",
        payload: { cancelRequestId: cancel.cancelRequestId, reason: cancel.reason, requestedAt: cancel.requestedAt, expectedRevision: cancel.expectedRevision },
        terminal: { state: "cancelled", reasonCode: "MODEL_CANCELLED" },
      });
    }
    return this.events;
  }

  private finish(args: { type: KernelEventType; payload: JsonObject; terminal?: AdapterEvent["terminal"] }): void {
    if (this.terminal) return;
    this.terminal = args.terminal;
    this.emit(args.type, args.payload, args.terminal);
  }

  private emit(type: KernelEventType, payload: JsonObject, terminal?: AdapterEvent["terminal"]): void {
    const event: AdapterEvent = {
      schemaVersion: F04_SCHEMA_VERSION,
      eventId: `event-${this.seq + 1}`,
      runtimeEventId: `runtime-${this.seq + 1}`,
      seq: ++this.seq,
      taskId: this.currentTaskId,
      operationKey: this.currentOperationKey,
      requestId: this.requestId,
      occurredAt: this.clock(),
      source: "f04-adapter",
      type,
      payload,
      ...(terminal ? { terminal } : {}),
    };
    this.events.push(event);
  }

  private currentTaskId = "";
  private currentOperationKey = "";
}

function parseObject(json: string): JsonObject {
  let value: unknown;
  try {
    value = JSON.parse(json);
  } catch {
    throw new AdapterError("MODEL_TOOL_ARGUMENTS_INVALID", "tool arguments are not valid JSON", { json });
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AdapterError("MODEL_TOOL_ARGUMENTS_INVALID", "tool arguments must be a JSON object");
  }
  return value as JsonObject;
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && (error.name === "AbortError" || /aborted|cancelled/i.test(error.message));
}

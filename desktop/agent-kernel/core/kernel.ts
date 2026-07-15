import { checkpointChecksum, verifyCheckpointChecksum } from "./checkpoint.ts";
import {
  KernelError,
  asJsonValue,
  isKernelError,
  normalizeKernelError,
} from "./errors.ts";
import { assertSameBuildIdentity, validateBuildIdentity } from "./identity.ts";
import type {
  AgentModelEvent,
  AgentModelRequest,
  AgentResourceBudget,
  AgentTurnInput,
  BudgetState,
  CancelReason,
  CanonicalContentBlock,
  CanonicalMessage,
  GrantSnapshot,
  KernelAuditEntry,
  KernelBuildIdentity,
  KernelCheckpoint,
  KernelDeps,
  KernelEventEnvelope,
  KernelEventType,
  KernelSession,
  ModelContext,
  ModelRuntimeSnapshot,
  OpenSessionInput,
  ToolInvocationContext,
} from "./types.ts";

const FORBIDDEN_EMBEDDED_KEYS = new Set([
  "runner_model",
  "runner_base_url",
  "baseUrl",
  "base_url",
  "apiKey",
  "api_key",
  "rawCredential",
  "raw_credential",
  "permissionMode",
  "permission_mode",
  "globalConfig",
  "global_config",
  "HOME",
  "PATH",
]);

const TERMINAL_CODES = new Set(["MODEL_CANCELLED", "TOOL_CANCELLED", "GRANT_REVOKED"]);

type TerminalRecord = {
  state: "succeeded" | "failed" | "cancelled";
  reasonCode: string;
};

type ToolState = {
  index: number;
  toolUseId: string;
  name: string;
  argumentJson: string;
  completed: boolean;
};

type TurnState = {
  turnId: string;
  input: AgentTurnInput;
  controller: AbortController;
  cancelReason?: CancelReason;
  terminal?: TerminalRecord;
  budget: BudgetState;
};

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => stableJson(item)).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(object[key])}`)
    .join(",")}}`;
}

function sameValue(left: unknown, right: unknown): boolean {
  return stableJson(left) === stableJson(right);
}

function scanForbiddenKeys(value: unknown, seen = new WeakSet<object>()): string | undefined {
  if (value === null || typeof value !== "object") return undefined;
  if (seen.has(value)) return undefined;
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = scanForbiddenKeys(item, seen);
      if (found) return found;
    }
    return undefined;
  }
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_EMBEDDED_KEYS.has(key)) return key;
    const found = scanForbiddenKeys(child, seen);
    if (found) return found;
  }
  return undefined;
}

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function userContentBlocks(input: AgentTurnInput): CanonicalContentBlock[] {
  if (typeof input.userMessage === "string") return [{ type: "text", text: input.userMessage }];
  return input.userMessage.map((block) => ({ ...block }));
}

function validateBudget(budget: AgentResourceBudget): void {
  const positive = [
    budget.wallSeconds,
    budget.maxTurns,
    budget.maxToolCalls,
    budget.maxModelInputTokens,
    budget.maxModelOutputTokens,
    budget.maxToolOutputBytes,
    budget.maxArtifactBytes,
    budget.maxConcurrentTools,
  ];
  if (positive.some((value) => !Number.isFinite(value) || value < 1)) {
    throw new KernelError("MODEL_CONFIG_INVALID", "resource budget is invalid");
  }
}

function validateGrant(grant: GrantSnapshot, taskId: string, now: string): void {
  if (grant.schemaVersion !== 1 || grant.taskId !== taskId || grant.revision < 1) {
    throw new KernelError("GRANT_REVISION_MISMATCH", "grant snapshot is not bound to the task");
  }
  const expiresAt = Date.parse(grant.expiresAt);
  const issuedAt = Date.parse(grant.issuedAt);
  if (!Number.isFinite(expiresAt) || !Number.isFinite(issuedAt) || expiresAt <= issuedAt || expiresAt <= Date.parse(now)) {
    throw new KernelError("GRANT_EXPIRED", "grant snapshot is expired or invalid");
  }
}

function validateModelSnapshot(model: ModelRuntimeSnapshot, expected: ModelRuntimeSnapshot): void {
  if (model.schemaVersion !== 1 || model.revision < 1 || !model.credentialHandle) {
    throw new KernelError("MODEL_CONFIG_INVALID", "model runtime snapshot is invalid");
  }
  if (!sameValue(model, expected)) {
    throw new KernelError("MODEL_CONFIG_REVISION_MISSING", "model runtime snapshot changed after binding");
  }
  if (!model.capabilities.streaming || !model.capabilities.toolUse) {
    throw new KernelError("MODEL_CAPABILITY_PROBE_FAILED", "bound model lacks streaming or tool capability");
  }
}

function validateTurn(input: AgentTurnInput, taskId: string, operationKey: string, grant: GrantSnapshot, now: string): void {
  const forbidden = scanForbiddenKeys(input);
  if (forbidden) throw new KernelError("KERNEL_INPUT_INVALID", "embedded turn input contains a forbidden field", { field: forbidden });
  if (
    input.schemaVersion !== 1 ||
    input.taskId !== taskId ||
    input.operationKey !== operationKey ||
    !input.systemPrompt ||
    !input.outputContract ||
    !input.context
  ) {
    throw new KernelError("KERNEL_INPUT_INVALID", "turn identity or required input is invalid");
  }
  if (utf8Bytes(input.systemPrompt) > 256 * 1024 || utf8Bytes(typeof input.userMessage === "string" ? input.userMessage : JSON.stringify(input.userMessage)) > 4 * 1024 * 1024) {
    throw new KernelError("KERNEL_INPUT_INVALID", "turn input exceeds the embedded size limit");
  }
  const deadline = Date.parse(input.deadlineAt);
  const grantExpiry = Date.parse(grant.expiresAt);
  if (!Number.isFinite(deadline) || deadline <= Date.parse(now) || deadline >= grantExpiry) {
    throw new KernelError("MODEL_TIMEOUT", "turn deadline must be valid and precede grant expiry");
  }
}

function parseJsonObject(json: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    throw new KernelError("MODEL_TOOL_ARGUMENTS_INVALID", "tool arguments are not valid JSON");
  }
  if (!isJsonObject(parsed)) throw new KernelError("MODEL_TOOL_ARGUMENTS_INVALID", "tool arguments must be a JSON object");
  return parsed;
}

function isAbortError(error: unknown): boolean {
  return isJsonObject(error) && error.name === "AbortError";
}

function cancelCode(reason: CancelReason): string {
  return reason === "grant_revoked" ? "GRANT_REVOKED" : "MODEL_CANCELLED";
}

class KernelSessionImpl implements KernelSession {
  private readonly taskId: string;
  private readonly operationKey: string;
  private activeTurn: TurnState | undefined;
  private closed = false;
  private readonly history: CanonicalMessage[];
  private readonly budget: AgentResourceBudget;
  private readonly deps: KernelDeps;
  private readonly modelSnapshot: ModelRuntimeSnapshot;
  private readonly grant: GrantSnapshot;

  constructor(
    taskId: string,
    operationKey: string,
    modelSnapshot: ModelRuntimeSnapshot,
    grant: GrantSnapshot,
    budget: AgentResourceBudget,
    deps: KernelDeps,
    resume?: KernelCheckpoint,
  ) {
    this.taskId = taskId;
    this.operationKey = operationKey;
    this.modelSnapshot = Object.freeze({ ...modelSnapshot });
    this.grant = Object.freeze({ ...grant });
    this.budget = Object.freeze({ ...budget });
    this.deps = deps;
    this.history = resume?.messages.map((message) => ({ ...message, content: message.content.map((block) => ({ ...block })) })) ?? [];
  }

  runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    this.ensureOpen();
    if (this.activeTurn) throw new KernelError("KERNEL_TURN_ALREADY_ACTIVE", "a turn is already active");
    validateTurn(input, this.taskId, this.operationKey, this.grant, this.deps.clock.now());
    const turn: TurnState = {
      turnId: this.deps.ids.next("turn"),
      input,
      controller: new AbortController(),
      budget: { turnsUsed: 0, toolCallsUsed: 0, modelInputTokens: 0, modelOutputTokens: 0 },
    };
    this.activeTurn = turn;
    return this.executeTurn(turn);
  }

  async checkpoint(): Promise<KernelCheckpoint> {
    this.ensureOpen();
    const checkpointBody = {
      schemaVersion: 1 as const,
      checkpointId: this.deps.ids.next("checkpoint"),
      taskId: this.taskId,
      operationKey: this.operationKey,
      modelConfigRevision: this.modelSnapshot.revision,
      grantRevision: this.grant.revision,
      lastDurableEventSeq: await this.deps.session.currentDurableEventSeq(),
      messages: this.history.map((message) => ({ ...message, content: message.content.map((block) => ({ ...block })) })),
      compactState: { schemaVersion: 1 as const, strategy: "none" as const, summaryHash: null, messageCountAtBoundary: this.history.length },
      budgetState: this.activeTurn?.budget ?? { turnsUsed: 0, toolCallsUsed: 0, modelInputTokens: 0, modelOutputTokens: 0 },
      createdAt: this.deps.clock.now(),
    };
    const checkpoint: KernelCheckpoint = { ...checkpointBody, checksum: await checkpointChecksum(checkpointBody) };
    await this.deps.session.saveCheckpoint(checkpoint);
    await this.emitAudit("session_lifecycle", { action: "checkpoint_saved", checkpointId: checkpoint.checkpointId });
    return checkpoint;
  }

  async cancel(reason: CancelReason): Promise<void> {
    this.ensureOpen();
    const turn = this.activeTurn;
    if (!turn || turn.terminal) return;
    if (!turn.cancelReason) turn.cancelReason = reason;
    turn.controller.abort();
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const turn = this.activeTurn;
    if (turn && !turn.terminal) {
      if (!turn.cancelReason) turn.cancelReason = "user";
      turn.controller.abort();
    }
    await this.deps.session.close();
    await this.emitAudit("session_lifecycle", { action: "closed" });
  }

  private ensureOpen(): void {
    if (this.closed) throw new KernelError("KERNEL_SESSION_CLOSED", "kernel session is closed");
  }

  private async emitEvent(_turn: TurnState, type: KernelEventType, payload: Record<string, unknown>): Promise<KernelEventEnvelope> {
    const event: KernelEventEnvelope = {
      schemaVersion: 1,
      taskId: this.taskId,
      operationKey: this.operationKey,
      runtimeEventId: this.deps.ids.next("event"),
      occurredAt: this.deps.clock.now(),
      type,
      payload: asJsonValue(payload) as Record<string, import("./types.ts").JsonValue>,
    };
    await this.deps.events.publish(event);
    await this.deps.telemetry.record("kernel.event", { type, runtimeEventId: event.runtimeEventId });
    return event;
  }

  private async emitAudit(kind: KernelAuditEntry["kind"], payload: Record<string, unknown>, turn?: TurnState): Promise<void> {
    const entry: KernelAuditEntry = {
      taskId: this.taskId,
      operationKey: this.operationKey,
      occurredAt: this.deps.clock.now(),
      kind,
      payload: asJsonValue(payload) as Record<string, import("./types.ts").JsonValue>,
    };
    await this.deps.events.audit(entry);
    await this.deps.telemetry.record("kernel.audit", { kind, turnId: turn?.turnId ?? "session" });
  }

  private async terminalEvent(
    turn: TurnState,
    state: TerminalRecord["state"],
    type: "agent.turn.completed" | "agent.turn.failed" | "agent.turn.cancelled",
    reasonCode: string,
    payload: Record<string, unknown> = {},
  ): Promise<KernelEventEnvelope | undefined> {
    if (turn.terminal) {
      await this.emitAudit("late_terminal", { eventType: type, state, reasonCode, firstState: turn.terminal.state }, turn);
      return undefined;
    }
    turn.terminal = { state, reasonCode };
    return this.emitEvent(turn, type, { ...payload, state, reasonCode });
  }

  private async *executeTurn(turn: TurnState): AsyncIterable<KernelEventEnvelope> {
    const userMessage: CanonicalMessage = {
      messageId: turn.input.messageId ?? this.deps.ids.next("message"),
      role: "user",
      content: userContentBlocks(turn.input),
    };
    this.history.push(userMessage);
    try {
      const started = await this.emitEvent(turn, "agent.turn.started", { turnId: turn.turnId });
      yield started;
      while (!turn.terminal) {
        if (turn.cancelReason) {
          const cancelled = await this.terminalEvent(turn, "cancelled", "agent.turn.cancelled", cancelCode(turn.cancelReason), { cancelReason: turn.cancelReason });
          if (cancelled) yield cancelled;
          return;
        }
        if (turn.budget.turnsUsed >= this.budget.maxTurns) throw new KernelError("MODEL_UPSTREAM_ERROR", "turn budget exceeded");
        const context: ModelContext = await this.deps.context.buildModelContext(turn.input, this.history);
        const request: AgentModelRequest = {
          requestId: this.deps.ids.next("request"),
          taskId: this.taskId,
          purpose: this.modelSnapshot.purpose,
          configRevision: this.modelSnapshot.revision,
          routeId: this.modelSnapshot.routeId,
          model: this.modelSnapshot.model,
          system: context.system,
          messages: context.messages,
          tools: context.tools,
          toolChoice: context.toolChoice,
          maxOutputTokens: Math.min(this.budget.maxModelOutputTokens, this.modelSnapshot.limits.maxOutputTokens),
        };
        const counted = await this.deps.model.countTokens({ request });
        if (counted.inputTokens > this.budget.maxModelInputTokens) throw new KernelError("MODEL_CONTEXT_EXCEEDED", "model input budget exceeded");
        turn.budget.modelInputTokens += counted.inputTokens;
        const pending = new Map<number, ToolState>();
        const toolResults: Array<{ toolUseId: string; result: { content: string; isError: boolean } }> = [];
        const assistantBlocks: CanonicalContentBlock[] = [];
        let text = "";
        let sawStop = false;
        let completedToolCount = 0;
        for await (const event of this.deps.model.stream(request, turn.controller.signal)) {
          if (turn.cancelReason) {
            if (event.type === "message_stop") await this.emitAudit("late_terminal", { eventType: event.type, proposedState: "succeeded" }, turn);
            const cancelled = await this.terminalEvent(turn, "cancelled", "agent.turn.cancelled", cancelCode(turn.cancelReason), { cancelReason: turn.cancelReason });
            if (cancelled) yield cancelled;
            return;
          }
          this.validateModelEvent(event, request.requestId);
          switch (event.type) {
            case "message_start":
              break;
            case "text_delta": {
              text += event.text;
              yield await this.emitEvent(turn, "agent.message.delta", { text: event.text });
              break;
            }
            case "tool_start":
              if (pending.has(event.index) || !event.id || !event.name) throw new KernelError("MODEL_TOOL_CORRELATION_MISMATCH", "duplicate or incomplete tool request");
              pending.set(event.index, { index: event.index, toolUseId: event.id, name: event.name, argumentJson: "", completed: false });
              yield await this.emitEvent(turn, "agent.tool.requested", { toolUseId: event.id, name: event.name });
              break;
            case "tool_arguments_delta": {
              const tool = pending.get(event.index);
              if (!tool || tool.completed) throw new KernelError("MODEL_TOOL_CORRELATION_MISMATCH", "tool arguments do not correlate to an active tool");
              tool.argumentJson += event.json;
              break;
            }
            case "tool_stop": {
              const toolState = pending.get(event.index);
              if (!toolState || toolState.completed) throw new KernelError("MODEL_TOOL_CORRELATION_MISMATCH", "tool stop does not correlate to an active tool");
              if (turn.budget.toolCallsUsed >= this.budget.maxToolCalls) throw new KernelError("MODEL_UPSTREAM_ERROR", "tool call budget exceeded");
              const input = parseJsonObject(toolState.argumentJson);
              const tool = this.deps.tools.resolve(toolState.name);
              if (!tool) throw new KernelError("TOOL_NOT_REGISTERED", "requested tool is not registered");
              toolState.completed = true;
              turn.budget.toolCallsUsed += 1;
              const invocationContext: ToolInvocationContext = {
                taskId: this.taskId,
                operationKey: this.operationKey,
                grant: this.grant,
                signal: turn.controller.signal,
                requestId: request.requestId,
                toolUseId: toolState.toolUseId,
                context: turn.input.context,
              };
              assistantBlocks.push({ type: "tool_use", toolUseId: toolState.toolUseId, name: toolState.name, input: input as Record<string, import("./types.ts").JsonValue> });
              yield await this.emitEvent(turn, "agent.tool.started", { toolUseId: toolState.toolUseId, name: toolState.name });
              const validation = await tool.validate(input, invocationContext);
              if (!validation.allowed) {
                const reasonCode = validation.reasonCode ?? "TOOL_CAPABILITY_DENIED";
                if (reasonCode === "GRANT_REVOKED") {
                  turn.cancelReason = "grant_revoked";
                  turn.controller.abort();
                  throw new KernelError("GRANT_REVOKED", "grant was revoked during tool validation");
                }
                const result = { content: validation.message ?? "tool capability denied", isError: true };
                toolResults.push({ toolUseId: toolState.toolUseId, result });
                yield await this.emitEvent(turn, "agent.tool.denied", { toolUseId: toolState.toolUseId, reasonCode });
                continue;
              }
              try {
                const output = await tool.invoke(input, invocationContext);
                const result = tool.toModelResult(output);
                if (utf8Bytes(result.content) > this.budget.maxToolOutputBytes) throw new KernelError("TOOL_OUTPUT_LIMIT", "tool output budget exceeded");
                toolResults.push({ toolUseId: toolState.toolUseId, result });
                yield await this.emitEvent(turn, "agent.tool.completed", { toolUseId: toolState.toolUseId, isError: result.isError, output: result.content });
              } catch (error) {
                if (turn.cancelReason || isAbortError(error)) throw error;
                const normalized = normalizeKernelError(error, "TOOL_EXECUTION_FAILED", "tool execution failed");
                const result = { content: "tool execution failed", isError: true };
                toolResults.push({ toolUseId: toolState.toolUseId, result });
                yield await this.emitEvent(turn, "agent.tool.failed", { toolUseId: toolState.toolUseId, reasonCode: normalized.code });
              }
              completedToolCount += 1;
              break;
            }
            case "usage":
              turn.budget.modelInputTokens += event.inputTokens;
              turn.budget.modelOutputTokens += event.outputTokens;
              break;
            case "message_stop":
              sawStop = true;
              break;
            case "error":
              throw new KernelError("MODEL_UPSTREAM_ERROR", "model stream returned an error", { providerCode: event.code, retryable: event.retryable });
          }
        }
        if (!sawStop) throw new KernelError("MODEL_UPSTREAM_ERROR", "model stream ended without a terminal stop");
        turn.budget.turnsUsed += 1;
        if (text) assistantBlocks.unshift({ type: "text", text });
        if (assistantBlocks.length) this.history.push({ messageId: this.deps.ids.next("message"), role: "assistant", content: assistantBlocks });
        if (completedToolCount > 0) {
          for (const toolResult of toolResults) {
            this.history.push({
              messageId: this.deps.ids.next("message"),
              role: "user",
              content: [{ type: "tool_result", toolUseId: toolResult.toolUseId, result: toolResult.result }],
            });
          }
          if (text) yield await this.emitEvent(turn, "agent.message.completed", { text });
          continue;
        }
        if (text) yield await this.emitEvent(turn, "agent.message.completed", { text });
        const completed = await this.terminalEvent(turn, "succeeded", "agent.turn.completed", "END_TURN", { stopReason: "end_turn" });
        if (completed) yield completed;
        return;
      }
    } catch (error) {
      const shouldCancel = Boolean(turn.cancelReason) || isAbortError(error) || (isKernelError(error) && TERMINAL_CODES.has(error.code));
      if (shouldCancel) {
        const cancelled = await this.terminalEvent(turn, "cancelled", "agent.turn.cancelled", cancelCode(turn.cancelReason ?? "user"), { cancelReason: turn.cancelReason ?? "user" });
        if (cancelled) yield cancelled;
      } else {
        const normalized = normalizeKernelError(error, "MODEL_UPSTREAM_ERROR", "kernel turn failed");
        const failed = await this.terminalEvent(turn, "failed", "agent.turn.failed", normalized.code, { errorCode: normalized.code, details: normalized.details });
        if (failed) yield failed;
      }
    } finally {
      if (this.activeTurn === turn) this.activeTurn = undefined;
    }
  }

  private validateModelEvent(event: AgentModelEvent, requestId: string): void {
    if (event.schemaVersion !== 1) throw new KernelError("MODEL_EVENT_UNKNOWN", "model event schema version is unsupported");
    if (event.requestId !== requestId) throw new KernelError("MODEL_REQUEST_ID_MISMATCH", "model event request id does not match active request");
  }
}

export class EchoAgentKernel {
  private readonly buildIdentity: KernelBuildIdentity;

  constructor(identity: KernelBuildIdentity) {
    this.buildIdentity = validateBuildIdentity(identity);
  }

  identity(): KernelBuildIdentity {
    return this.buildIdentity;
  }

  async openSession(input: OpenSessionInput, deps: KernelDeps): Promise<KernelSession> {
    const startupIdentity = await deps.session.startup(this.buildIdentity);
    assertSameBuildIdentity(this.buildIdentity, startupIdentity);
    const forbidden = scanForbiddenKeys(input);
    if (forbidden) throw new KernelError("KERNEL_INPUT_INVALID", "open input contains a forbidden field", { field: forbidden });
    validateBudget(input.limits);
    validateGrant(input.grant, input.taskId, deps.clock.now());
    const actualSnapshot = deps.model.snapshot();
    validateModelSnapshot(actualSnapshot, input.model);
    if (input.resume) {
      if (input.resume.schemaVersion !== 1) throw new KernelError("CHECKPOINT_CORRUPT", "checkpoint schema version is unsupported");
      if (input.resume.taskId !== input.taskId) throw new KernelError("CHECKPOINT_TASK_MISMATCH", "checkpoint task identity does not match");
      if (input.resume.operationKey !== input.operationKey) throw new KernelError("CHECKPOINT_OPERATION_MISMATCH", "checkpoint operation identity does not match");
      if (input.resume.modelConfigRevision !== input.model.revision) throw new KernelError("CHECKPOINT_MODEL_REVISION_MISSING", "checkpoint model revision does not match");
      if (input.resume.grantRevision !== input.grant.revision) throw new KernelError("GRANT_REVISION_MISMATCH", "checkpoint grant revision does not match");
      if (input.resume.lastDurableEventSeq > await deps.session.currentDurableEventSeq()) throw new KernelError("CHECKPOINT_EVENT_SEQ_AHEAD", "checkpoint durable sequence is ahead of the session");
      await verifyCheckpointChecksum(input.resume);
    }
    return new KernelSessionImpl(input.taskId, input.operationKey, input.model, input.grant, input.limits, deps, input.resume);
  }
}

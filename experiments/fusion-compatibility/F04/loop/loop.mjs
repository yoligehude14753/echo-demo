/**
 * F04 bounded adapted loop.
 *
 * This is a task-owned experiment. It copies only the bounded loop shape
 * required by F04; it does not import the incomplete Claude snapshot or Echo
 * product/runtime code.
 */

export const CLAUDE_SNAPSHOT_SHA256 =
  "sha256:b1f141a4bd591335d2be4e347218d936a753041f8536a9b881c7ef7100b8416a";
export const TRACE_SCHEMA_VERSION = 1;
export const TRACE_SOURCE = "f04.bounded-adapted-loop";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "timeout"]);
const CANCEL_REASONS = new Set(["user", "timeout", "provider_error", "grant_revoked"]);

function copy(value) {
  return JSON.parse(JSON.stringify(value));
}
function requiredString(value, field) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${field} must be a non-empty string`);
  }
  return value;
}

function requiredPositiveInteger(value, field) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new TypeError(`${field} must be a positive integer`);
  }
  return value;
}

function validateTurnInput(input) {
  if (!input || typeof input !== "object") {
    throw new TypeError("turn input must be an object");
  }
  for (const field of ["taskId", "operationKey", "requestId", "grantId", "userText"]) {
    requiredString(input[field], field);
  }
  requiredPositiveInteger(input.grantRevision, "grantRevision");
  for (const forbidden of ["runnerModel", "runnerBaseUrl", "credential", "credentials"]) {
    if (forbidden in input) {
      throw new TypeError(`${forbidden} is forbidden at the embedded adapter boundary`);
    }
  }
}

function message({ messageId, parentMessageId, role, content, toolUseId, isError }) {
  const result = { messageId, parentMessageId, role, content };
  if (toolUseId !== undefined) result.toolUseId = toolUseId;
  if (isError !== undefined) result.isError = isError;
  return result;
}

export class DeterministicFakeModel {
  initial({ input }) {
    return {
      text: `读取 ${input.userText.includes("demo.txt") ? "demo.txt" : "fixture.txt"}`,
      toolCall: {
        toolUseId: "tool-1",
        name: "Read",
        input: { path: "demo.txt" },
      },
    };
  }

  continuation({ result }) {
    return {
      text: result.isError ? "工具已取消" : "已读取 demo.txt",
      stopReason: "end_turn",
      usage: { inputTokens: 12, outputTokens: 4, estimated: true },
      retryable: false,
    };
  }
}

export class DeterministicFakeToolRegistry {
  constructor() {
    this.invocations = [];
  }

  invoke(call, grant) {
    if (grant.grantRevision !== 7 || grant.grantId !== "grant-f04-001") {
      throw new Error("fake tool grant context mismatch");
    }
    this.invocations.push({
      toolUseId: call.toolUseId,
      name: call.name,
      input: copy(call.input),
      grant: copy(grant),
    });
    return {
      toolUseId: call.toolUseId,
      output: { kind: "text", text: "fixture contents" },
      isError: false,
    };
  }

  syntheticResult(call, reason) {
    return {
      toolUseId: call.toolUseId,
      output: { kind: "text", text: `tool interrupted: ${reason}` },
      isError: true,
      synthetic: true,
    };
  }
}

export class BoundedFusionLoop {
  constructor({ model = new DeterministicFakeModel(), tools = new DeterministicFakeToolRegistry() } = {}) {
    this.model = model;
    this.tools = tools;
    this.events = [];
    this.audits = [];
    this.state = "idle";
    this.input = null;
    this.grant = null;
    this.pendingCall = null;
    this.terminal = null;
  }

  startTurn(input) {
    if (this.state !== "idle") throw new Error(`cannot start from state ${this.state}`);
    validateTurnInput(input);
    this.input = copy(input);
    this.grant = { grantId: input.grantId, grantRevision: input.grantRevision };
    this.state = "active";
    this.emit("agent.turn.started", {
      modelRequest: {
        requestId: input.requestId,
        taskId: input.taskId,
        operationKey: input.operationKey,
        purpose: "turn",
        model: "fake/deterministic-v1",
        maxOutputTokens: 64,
      },
      userMessage: message({
        messageId: "msg-user-1",
        parentMessageId: null,
        role: "user",
        content: [{ type: "text", text: input.userText }],
      }),
    });

    const initial = this.model.initial({ input: this.input });
    this.emit("agent.message.delta", {
      phase: "initial",
      message: message({
        messageId: "msg-assistant-1",
        parentMessageId: "msg-user-1",
        role: "assistant",
        content: [{ type: "text", text: initial.text }],
      }),
      text: initial.text,
    });
    this.pendingCall = copy(initial.toolCall);
    this.emit("agent.tool.requested", {
      tool: copy(initial.toolCall),
      grant: copy(this.grant),
      message: message({
        messageId: "msg-assistant-tool-1",
        parentMessageId: "msg-assistant-1",
        role: "assistant",
        content: [{ type: "tool_use", ...initial.toolCall }],
        toolUseId: initial.toolCall.toolUseId,
      }),
    });
    return copy(this.pendingCall);
  }

  invokePendingTool() {
    if (this.terminal) {
      this.audit("tool_invocation_after_terminal", { state: this.terminal.state });
      return null;
    }
    if (!this.pendingCall) throw new Error("no pending tool call");
    return this.tools.invoke(copy(this.pendingCall), copy(this.grant));
  }

  resumeWithToolResult(result) {
    if (this.terminal) {
      this.audit("late_tool_result", { toolUseId: result?.toolUseId ?? null });
      return false;
    }
    if (!this.pendingCall) {
      return this.rejectMismatch({
        toolUseId: result?.toolUseId ?? "missing",
        expectedToolUseId: null,
      });
    }
    if (!result || result.toolUseId !== this.pendingCall.toolUseId) {
      return this.rejectMismatch({
        toolUseId: result?.toolUseId ?? "missing",
        expectedToolUseId: this.pendingCall.toolUseId,
      });
    }

    const call = this.pendingCall;
    this.pendingCall = null;
    this.emit("agent.tool.completed", {
      toolUseId: result.toolUseId,
      isError: result.isError,
      synthetic: result.synthetic === true,
      output: copy(result.output),
      grant: copy(this.grant),
      message: message({
        messageId: "msg-tool-result-1",
        parentMessageId: "msg-assistant-tool-1",
        role: "user",
        content: [{ type: "tool_result", toolUseId: result.toolUseId, output: copy(result.output) }],
        toolUseId: result.toolUseId,
        isError: result.isError,
      }),
    });
    this.emit("agent.model.continuation", {
      parentToolUseId: call.toolUseId,
      request: {
        requestId: this.input.requestId,
        taskId: this.input.taskId,
        operationKey: this.input.operationKey,
        purpose: "continuation",
      },
    });
    const continuation = this.model.continuation({ result: copy(result), input: this.input });
    this.emit("agent.message.delta", {
      phase: "continuation",
      message: message({
        messageId: "msg-assistant-2",
        parentMessageId: "msg-tool-result-1",
        role: "assistant",
        content: [{ type: "text", text: continuation.text }],
      }),
      text: continuation.text,
    });
    this.finish("agent.turn.completed", {
      state: "succeeded",
      stopReason: continuation.stopReason,
      usage: copy(continuation.usage),
      retryable: continuation.retryable,
    });
    return true;
  }

  rejectMismatch({ toolUseId, expectedToolUseId = this.pendingCall?.toolUseId ?? null }) {
    if (this.terminal) {
      this.audit("late_mismatch_rejection", { toolUseId });
      return false;
    }
    this.pendingCall = null;
    this.emit("agent.tool.rejected", {
      code: "MODEL_TOOL_CORRELATION_MISMATCH",
      toolUseId,
      expectedToolUseId,
      toolInvoked: false,
      retryable: false,
    });
    this.finish("agent.turn.failed", {
      state: "failed",
      code: "MODEL_TOOL_CORRELATION_MISMATCH",
      reasonCode: "MODEL_TOOL_CORRELATION_MISMATCH",
      retryable: false,
      toolInvoked: false,
    });
    return false;
  }

  cancel({ cancelRequestId, reason = "user", requestedAt = "2026-01-01T00:00:10.000Z", expectedRevision = 1 }) {
    requiredString(cancelRequestId, "cancelRequestId");
    requiredString(requestedAt, "requestedAt");
    requiredPositiveInteger(expectedRevision, "expectedRevision");
    if (!CANCEL_REASONS.has(reason)) throw new TypeError(`unsupported cancel reason: ${reason}`);
    if (this.terminal) {
      this.audit("cancel_after_terminal", { cancelRequestId, state: this.terminal.state });
      return false;
    }
    this.state = "cancel_requested";
    this.emit("agent.turn.cancel.requested", {
      cancelRequestId,
      reason,
      requestedAt,
      expectedRevision,
    });
    if (this.pendingCall) {
      const synthetic = this.tools.syntheticResult(this.pendingCall, reason);
      this.pendingCall = null;
      this.emit("agent.tool.completed", {
        toolUseId: synthetic.toolUseId,
        isError: true,
        synthetic: true,
        output: copy(synthetic.output),
        grant: copy(this.grant),
        message: message({
          messageId: "msg-tool-result-cancelled",
          parentMessageId: "msg-assistant-tool-1",
          role: "user",
          content: [{ type: "tool_result", toolUseId: synthetic.toolUseId, output: copy(synthetic.output) }],
          toolUseId: synthetic.toolUseId,
          isError: true,
        }),
      });
    }
    this.finish("agent.turn.cancelled", {
      state: "cancelled",
      reason,
      reasonCode: reason === "user" ? "USER_CANCELLED" : `CANCEL_${reason.toUpperCase()}`,
      cancelRequestId,
    });
    return true;
  }

  recordLateTerminal({ event = "agent.turn.completed", state = "succeeded", reasonCode = "late" } = {}) {
    if (!this.terminal) throw new Error("late terminal requires an existing terminal state");
    this.audit("late_terminal_ignored", {
      event,
      state,
      reasonCode,
      durableTerminal: copy(this.terminal),
    });
    return false;
  }

  finish(event, payload) {
    if (!TERMINAL_STATES.has(payload.state)) throw new Error(`invalid terminal state: ${payload.state}`);
    if (this.terminal) {
      this.recordLateTerminal({ event, state: payload.state, reasonCode: payload.reasonCode ?? "late" });
      return null;
    }
    this.terminal = { event, state: payload.state, reasonCode: payload.reasonCode ?? null };
    this.state = payload.state;
    return this.emit(event, payload, true);
  }

  emit(event, payload, terminal = false) {
    if (this.terminal && !terminal) {
      this.audit("event_after_terminal", { event });
      return null;
    }
    const seq = this.events.length + 1;
    const kernelEvent = {
      schemaVersion: TRACE_SCHEMA_VERSION,
      eventId: `f04-${this.input.taskId}-${String(seq).padStart(3, "0")}`,
      seq,
      taskId: this.input.taskId,
      operationKey: this.input.operationKey,
      requestId: this.input.requestId,
      event,
      payload: copy(payload),
      source: TRACE_SOURCE,
      emittedAt: `2026-01-01T00:00:00.${String(seq).padStart(3, "0")}Z`,
      terminal,
    };
    this.events.push(kernelEvent);
    return kernelEvent;
  }

  audit(kind, detail) {
    this.audits.push({
      kind,
      detail: copy(detail),
      source: TRACE_SOURCE,
      recordedAt: `2026-01-01T00:01:00.${String(this.audits.length + 1).padStart(3, "0")}Z`,
    });
  }

  trace(caseName) {
    return {
      traceSchemaVersion: TRACE_SCHEMA_VERSION,
      traceId: `f04-${caseName}-${this.input.operationKey}`,
      source: "echo",
      case: caseName,
      adaptedFrom: CLAUDE_SNAPSHOT_SHA256,
      identity: {
        task_id: this.input.taskId,
        operation_key: this.input.operationKey,
        request_id: this.input.requestId,
      },
      canonical_events: copy(this.events),
      terminal: copy(this.terminal),
      audits: copy(this.audits),
      tool_invocations: copy(this.tools.invocations),
    };
  }
}

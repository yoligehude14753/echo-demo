import { threadId } from "node:worker_threads";
import { createKernelWorkerRuntime } from "../../worker/bridge.ts";
import { EchoAgentKernel } from "../../../../agent-kernel/core/index.ts";

const modelSnapshot = {
  schemaVersion: 1,
  revision: 1,
  configHash: "b04k-test-config",
  purpose: "agent_main",
  routeId: "b04k-test-route",
  protocol: "openai_chat",
  model: "b04k-test-model",
  capabilities: {
    streaming: true,
    toolUse: true,
    parallelToolUse: false,
    toolChoice: false,
    systemMessages: true,
    usageInStream: true,
    promptCache: false,
    multimodalImages: false,
    multimodalDocuments: false,
  },
  limits: {
    contextWindow: 8192,
    maxOutputTokens: 128,
    requestTimeoutSeconds: 30,
    maxRetries: 0,
  },
  tokenizer: { kind: "conservative_estimate", identifier: "b04k-test", estimated: true, safetyMarginTokens: 16 },
  reasoning: { mode: "none", stripThinkTags: false, tokenBudget: null },
  credentialHandle: "b04k-test-handle",
};

let id = 0;
const ids = {
  next(kind) {
    id += 1;
    return `${kind}-b04k-${id}`;
  },
};

function makeDeps() {
  const compactContextMessages = [
    {
      messageId: "fixture-tool-assistant-old",
      role: "assistant",
      content: [{ type: "tool_use", toolUseId: "fixture-tool-old", name: "Read", input: {} }],
    },
    {
      messageId: "fixture-tool-result-old",
      role: "user",
      content: [{ type: "tool_result", toolUseId: "fixture-tool-old", result: { content: "old tool result".repeat(20), isError: false } }],
    },
    {
      messageId: "fixture-tool-assistant-recent",
      role: "assistant",
      content: [{ type: "tool_use", toolUseId: "fixture-tool-recent", name: "Read", input: {} }],
    },
    {
      messageId: "fixture-tool-result-recent",
      role: "user",
      content: [{ type: "tool_result", toolUseId: "fixture-tool-recent", result: { content: "recent tool result", isError: false } }],
    },
  ];
  const model = {
    snapshot: () => modelSnapshot,
    async countTokens() {
      return { inputTokens: 1, estimated: true };
    },
    async *stream(request) {
      yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
      yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "production-worker" };
      yield { schemaVersion: 1, type: "usage", requestId: request.requestId, inputTokens: 1, outputTokens: 1, cacheReadTokens: 0, estimated: true };
      yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
    },
  };
  const session = {
    async startup(identity) {
      return identity;
    },
    async currentDurableEventSeq() {
      return 100;
    },
    async saveCheckpoint() {},
    async close() {},
  };
  const events = {
    async publish() {},
    async audit() {},
  };
  const context = {
    async buildModelContext(_input, history) {
      return { system: [{ type: "text", text: "test" }], messages: [...compactContextMessages, ...history], tools: [] };
    },
  };
  return {
    model,
    tools: { list: () => [], resolve: () => undefined },
    session,
    events,
    context,
    clock: { now: () => "2026-07-15T00:00:00.000Z" },
    ids,
    telemetry: { async record() {} },
  };
}

export async function createWorkerRuntime({ open, identity }) {
  const kernel = new EchoAgentKernel(identity);
  const runtime = await createKernelWorkerRuntime(kernel, open, makeDeps());
  return {
    async *runTurn(input) {
      for await (const event of runtime.runTurn(input)) {
        yield {
          ...event,
          payload: { ...event.payload, workerPid: process.pid, workerThreadId: threadId },
        };
      }
    },
    checkpoint: () => runtime.checkpoint(),
    cancel: (reason) => runtime.cancel(reason),
    close: () => runtime.close(),
  };
}

export { modelSnapshot };

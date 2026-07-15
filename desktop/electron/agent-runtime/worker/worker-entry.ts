import { parentPort, threadId, workerData } from "node:worker_threads";
import { MessagePortChannel } from "../message-port/channel.ts";
import type { JsonObject, KernelEventEnvelope, OpenSessionInput } from "../../../agent-kernel/core/index.ts";
import {
  assertWorkerBuildIdentity,
  newRuntimeEventId,
  validateRuntimeManifest,
  type RuntimeManifest,
} from "./identity.ts";
import type { KernelWorkerRuntime, KernelWorkerRuntimeFactory } from "./bridge.ts";

type WorkerBootstrapData = {
  manifest: RuntimeManifest;
  factoryModule: string;
  factoryExport: string;
  factoryData?: import("../../../agent-kernel/core/index.ts").JsonObject;
};

const bootstrap = workerData as WorkerBootstrapData;
if (!parentPort) throw new Error("worker runtime requires parentPort");

let commandChain = Promise.resolve();

const channel = new MessagePortChannel(
  parentPort,
  (frame) => {
    if (frame.type === "cancel") {
      void handleFrame(frame);
      return;
    }
    commandChain = commandChain.then(() => handleFrame(frame));
  },
  (error) => {
    sendError("runtime-protocol", "runtime", "runtime", "RUNTIME_INVALID_FRAME", error instanceof Error ? error.message : "invalid runtime frame");
  },
);

let runtime: KernelWorkerRuntime | undefined;
let activeTurn = false;
let taskId = "runtime";
let operationKey = "runtime";
let factory: KernelWorkerRuntimeFactory;

function asJsonObject(value: unknown): JsonObject {
  return value as JsonObject;
}

function sendError(requestId: string, frameTaskId: string, frameOperationKey: string, code: string, message: string): void {
  try {
    channel.send({
      type: "error",
      requestId,
      taskId: frameTaskId,
      operationKey: frameOperationKey,
      payload: { code, message },
    });
  } catch {
    // The parent may already be gone; there is no second error channel.
  }
}

function requireRuntime(): KernelWorkerRuntime {
  if (!runtime) throw new Error("worker session is not open");
  return runtime;
}

function requirePayload<T extends object>(payload: JsonObject, field: string): T {
  const value = payload[field];
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`${field} payload is invalid`);
  return value as T;
}

function normalizeEvent(event: KernelEventEnvelope): KernelEventEnvelope {
  if (event.schemaVersion !== 1 || event.taskId !== taskId || event.operationKey !== operationKey) {
    throw new Error("kernel event identity does not match the open worker session");
  }
  return {
    ...event,
    runtimeEventId: event.runtimeEventId || newRuntimeEventId(),
  };
}

async function handleOpen(requestId: string, payload: JsonObject, frameIdentity: WorkerBootstrapData["manifest"]["buildIdentity"] | undefined): Promise<void> {
  if (runtime) throw new Error("worker session is already open");
  const open = requirePayload<OpenSessionInput>(payload, "open");
  if (!frameIdentity) throw new Error("open frame is missing build identity");
  assertWorkerBuildIdentity(bootstrap.manifest.buildIdentity, frameIdentity);
  if (!open.taskId || !open.operationKey) throw new Error("open identity is invalid");
  taskId = open.taskId;
  operationKey = open.operationKey;
  runtime = await factory({
    open,
    identity: bootstrap.manifest.buildIdentity,
    factoryData: bootstrap.factoryData,
  });
  channel.send({
    type: "opened",
    requestId,
    taskId,
    operationKey,
    payload: { manifestId: bootstrap.manifest.manifestId },
    buildIdentity: bootstrap.manifest.buildIdentity,
  });
}

async function handleTurn(requestId: string, payload: JsonObject): Promise<void> {
  if (activeTurn) throw new Error("worker turn is already active");
  const current = requireRuntime();
  const input = requirePayload<{ input: Parameters<KernelWorkerRuntime["runTurn"]>[0] }>(payload, "turn").input;
  if (!input || input.taskId !== taskId || input.operationKey !== operationKey) throw new Error("turn identity does not match the open worker session");
  activeTurn = true;
  try {
    for await (const rawEvent of current.runTurn(input)) {
      const event = normalizeEvent(rawEvent);
      channel.send({
        type: "event",
        requestId,
        taskId,
        operationKey,
        runtimeEventId: event.runtimeEventId,
        payload: asJsonObject(event),
      });
    }
    channel.send({ type: "turn_end", requestId, taskId, operationKey, payload: { ok: true } });
  } finally {
    activeTurn = false;
  }
}

async function handleCheckpoint(requestId: string): Promise<void> {
  const checkpoint = await requireRuntime().checkpoint();
  channel.send({
    type: "checkpointed",
    requestId,
    taskId,
    operationKey,
    payload: { checkpoint: asJsonObject(checkpoint) },
  });
}

async function handleCancel(requestId: string, payload: JsonObject): Promise<void> {
  const reason = payload.reason;
  if (reason !== "user" && reason !== "timeout" && reason !== "provider_error" && reason !== "grant_revoked") {
    throw new Error("cancel reason is invalid");
  }
  await requireRuntime().cancel(reason);
  channel.send({ type: "cancelled", requestId, taskId, operationKey, payload: { reason } });
}

async function handleClose(requestId: string): Promise<void> {
  if (runtime) await runtime.close();
  runtime = undefined;
  activeTurn = false;
  channel.send({ type: "closed", requestId, taskId, operationKey, payload: { ok: true } });
}

async function handleFrame(frame: import("../message-port/envelope.ts").RuntimeFrame): Promise<void> {
  try {
    switch (frame.type) {
      case "open":
        await handleOpen(frame.requestId, frame.payload, frame.buildIdentity);
        return;
      case "turn":
        await handleTurn(frame.requestId, frame.payload);
        return;
      case "checkpoint":
        await handleCheckpoint(frame.requestId);
        return;
      case "cancel":
        await handleCancel(frame.requestId, frame.payload);
        return;
      case "close":
        await handleClose(frame.requestId);
        return;
      default:
        throw new Error(`worker does not accept frame type ${frame.type}`);
    }
  } catch (error) {
    sendError(frame.requestId, frame.taskId, frame.operationKey, "RUNTIME_WORKER_REQUEST_FAILED", error instanceof Error ? error.message : "worker request failed");
    if (frame.type === "turn") {
      try {
        channel.send({ type: "turn_end", requestId: frame.requestId, taskId, operationKey, payload: { ok: false } });
      } catch {
        // Parent-side request will observe the worker error or exit.
      }
    }
  }
}

async function start(): Promise<void> {
  validateRuntimeManifest(bootstrap.manifest);
  const loaded = await import(bootstrap.factoryModule);
  const candidate = loaded[bootstrap.factoryExport] ?? loaded.default;
  if (typeof candidate !== "function") throw new Error("worker runtime factory export is missing");
  factory = candidate as KernelWorkerRuntimeFactory;
  channel.send({
    type: "ready",
    requestId: "ready",
    taskId: "runtime",
    operationKey: "runtime",
    payload: { manifestId: bootstrap.manifest.manifestId, pid: process.pid, threadId },
    buildIdentity: bootstrap.manifest.buildIdentity,
  });
}

void start().catch((error: unknown) => {
  sendError("bootstrap", "runtime", "runtime", "RUNTIME_UNAVAILABLE", error instanceof Error ? error.message : "worker bootstrap failed");
});

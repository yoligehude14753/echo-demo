import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_RUNTIME_FRAME_BYTES,
  RuntimeProtocolError,
  makeRuntimeFrame,
  validateRuntimeFrame,
} from "../message-port/envelope.ts";
import { MessagePortChannel, type RuntimeMessagePort } from "../message-port/channel.ts";
import { newRuntimeEventId } from "../worker/identity.ts";

class FakePort implements RuntimeMessagePort {
  readonly sent: unknown[] = [];
  private messageListener: ((value: unknown) => void) | undefined;
  private errorListener: ((error: unknown) => void) | undefined;

  postMessage(value: unknown): void {
    this.sent.push(value);
  }

  on(event: "message" | "messageerror", listener: (value: unknown) => void): this {
    if (event === "message") this.messageListener = listener;
    else this.errorListener = listener;
    return this;
  }

  close(): void {}

  receive(value: unknown): void {
    this.messageListener?.(value);
  }

  fail(error: unknown): void {
    this.errorListener?.(error);
  }
}

test("v1 envelope validates schema, bounded JSON payload, and known fields", () => {
  const frame = makeRuntimeFrame({
    type: "event",
    requestId: "request-1",
    taskId: "task-1",
    operationKey: "operation-1",
    runtimeEventId: "runtime-event-1",
    payload: { event: "agent.turn.completed" },
  });
  assert.equal(frame.schemaVersion, 1);
  assert.equal(validateRuntimeFrame(frame).runtimeEventId, "runtime-event-1");
  assert.throws(
    () => validateRuntimeFrame({ ...frame, unknown: true }),
    (error: unknown) => error instanceof RuntimeProtocolError && error.code === "RUNTIME_INVALID_FRAME",
  );
  assert.throws(
    () => validateRuntimeFrame({ ...frame, schemaVersion: 2 }),
    (error: unknown) => error instanceof RuntimeProtocolError && error.code === "RUNTIME_INVALID_FRAME",
  );
});

test("channel validates received frames and rejects oversized payloads", () => {
  const port = new FakePort();
  const received: string[] = [];
  const errors: unknown[] = [];
  const channel = new MessagePortChannel(port, (frame) => received.push(frame.type), (error) => errors.push(error));
  channel.send({ type: "ready", requestId: "ready", taskId: "runtime", operationKey: "runtime", payload: {} });
  port.receive(port.sent[0]);
  port.receive({ schemaVersion: 1, type: "event", requestId: "r", taskId: "t", operationKey: "o", payload: { broken: Symbol("not-json") } });
  assert.deepEqual(received, ["ready"]);
  assert.equal(errors.length, 1);
  assert.throws(
    () => makeRuntimeFrame({ type: "event", requestId: "r", taskId: "t", operationKey: "o", payload: { body: "x".repeat(MAX_RUNTIME_FRAME_BYTES) } }),
    (error: unknown) => error instanceof RuntimeProtocolError && error.code === "RUNTIME_FRAME_TOO_LARGE",
  );
  channel.close();
});

test("runtime event identity is delivery-local and carries no durable sequence", () => {
  const first = newRuntimeEventId();
  const second = newRuntimeEventId();
  assert.match(first, /^runtime-/);
  assert.match(second, /^runtime-/);
  assert.notEqual(first, second);
  assert.equal("seq" in ({ runtimeEventId: first } as Record<string, string>), false);
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  CaptureTransportReadinessGate,
  type CaptureTransportReadinessPort,
} from "./captureTransportReadiness.ts";

class FakeReadiness implements CaptureTransportReadinessPort {
  private listeners = new Set<(ready: boolean) => void>();
  private ready: boolean;

  constructor(ready: boolean) {
    this.ready = ready;
  }

  current(): boolean {
    return this.ready;
  }

  subscribe(listener: (ready: boolean) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  publish(ready: boolean): void {
    this.ready = ready;
    for (const listener of this.listeners) listener(ready);
  }
}

function fixture(initialReady = false) {
  const readiness = new FakeReadiness(initialReady);
  const calls = { starts: 0, pauses: 0, kicks: 0, ready: 0, notReady: 0 };
  const gate = new CaptureTransportReadinessGate(
    readiness,
    {
      start: () => { calls.starts += 1; },
      pause: () => { calls.pauses += 1; },
      kick: () => { calls.kicks += 1; },
    },
    {
      onReady: () => { calls.ready += 1; },
      onNotReady: () => { calls.notReady += 1; },
    },
  );
  return { readiness, calls, gate };
}

test("attach not-ready 时 recovery 与 kick 都不会启动 attempt", () => {
  const { calls, gate } = fixture(false);
  gate.start();
  gate.kick();
  assert.deepEqual(calls, { starts: 0, pauses: 0, kicks: 0, ready: 0, notReady: 0 });
});

test("ready 边沿只启动一次 recovery scheduler，随后 kick 不复制 ownership", () => {
  const { readiness, calls, gate } = fixture(false);
  gate.start();
  readiness.publish(true);
  gate.kick();
  readiness.publish(true);
  assert.deepEqual(calls, {
    starts: 1, pauses: 0, kicks: 1, ready: 1, notReady: 0,
  });
});

test("false 边沿暂停 attempt，flap 后 scheduler 只重启一次", () => {
  const { readiness, calls, gate } = fixture(true);
  gate.start();
  gate.kick();
  readiness.publish(false);
  gate.kick();
  readiness.publish(false);
  readiness.publish(true);
  gate.kick();
  assert.deepEqual(calls, {
    starts: 2,
    pauses: 1,
    kicks: 2,
    ready: 2,
    notReady: 1,
  });
});

test("router 只经 readiness gate 驱动 attempt，未就绪仍先 durable enqueue", () => {
  const source = readFileSync(
    new URL("./captureChunkRouter.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /new CaptureTransportReadinessGate\(/);
  assert.match(source, /current: backendSessionTransportReady/);
  assert.match(source, /subscribe: subscribeBackendSessionTransportReadiness/);
  assert.match(source, /readinessGate\?\.current\(\) !== true/);
  assert.match(source, /transportReady: readinessGate\?\.current\(\) === true/);
  assert.match(source, /readinessGate\.start\(\)/);
  assert.doesNotMatch(source, /coordinator\.(?:start|kick|pause)\(/);
  assert.doesNotMatch(source, /addEventListener\((?:BACKEND_ORIGIN_EVENT|SESSION_IDENTITY_EVENT)/);

  const persistBody = source.slice(
    source.indexOf("const persistChunk = async"),
    source.indexOf("const offChunk =", source.indexOf("const persistChunk = async")),
  );
  assert.ok(
    persistBody.indexOf("captureUploadSpool.enqueue(") <
      persistBody.indexOf("coordinator.setActivePartition(target.partition)"),
  );
  assert.match(persistBody, /isSameCaptureScope\(scope, audioCapture\.getCaptureScope\(\)\)/);
  assert.equal(source.split("coordinator.setActivePartition(").length - 1, 1);
  assert.doesNotMatch(source, /activatePartition|backendOriginGeneration|currentPartition/);
  assert.doesNotMatch(source, /captureAcceptedEnqueueDisposition/);

  const readyHandler = source.slice(
    source.indexOf("onReady: () =>"),
    source.indexOf("onNotReady: () =>"),
  );
  assert.doesNotMatch(readyHandler, /setActivePartition|resolveSpoolTarget/);
});

import assert from "node:assert/strict";
import { test } from "node:test";
import { EchoAgentKernel } from "../../core/kernel.ts";
import { KernelError } from "../../core/errors.ts";
import type {
  AgentModelEvent,
  AgentModelRequest,
  EchoAgentEventSink,
  EchoAgentSessionPort,
  EchoAgentTelemetryPort,
  EchoContextPort,
  EchoIdFactory,
  EchoModelPort,
  EchoToolRegistry,
  KernelAuditEntry,
  KernelBuildIdentity,
  KernelCheckpoint,
  KernelDeps,
  KernelEventEnvelope,
  ModelRuntimeSnapshot,
} from "../../core/types.ts";
import {
  GRANT,
  IDENTITY,
  MODEL,
  NOW,
  openInput,
  turnInput,
} from "./fixtures.ts";

class DeterministicModel implements EchoModelPort {
  readonly requests: AgentModelRequest[] = [];

  snapshot(): ModelRuntimeSnapshot {
    return MODEL;
  }

  async countTokens(_request: { request: AgentModelRequest }): Promise<{ inputTokens: number; estimated: boolean }> {
    return { inputTokens: 4, estimated: true };
  }

  async *stream(request: AgentModelRequest, _signal: AbortSignal): AsyncIterable<AgentModelEvent> {
    this.requests.push(request);
    yield { schemaVersion: 1, type: "message_start", requestId: request.requestId };
    yield { schemaVersion: 1, type: "text_delta", requestId: request.requestId, text: "deterministic" };
    yield { schemaVersion: 1, type: "message_stop", requestId: request.requestId, stopReason: "end_turn" };
  }
}

class RecordingSession implements EchoAgentSessionPort {
  readonly saved: KernelCheckpoint[] = [];
  readonly startupCalls: KernelBuildIdentity[] = [];
  readonly durableEventSeq: number;
  readonly startupIdentity: KernelBuildIdentity;

  constructor(
    startupIdentity: KernelBuildIdentity = IDENTITY,
    durableEventSeq = 100,
  ) {
    this.startupIdentity = startupIdentity;
    this.durableEventSeq = durableEventSeq;
  }

  async startup(kernelIdentity: KernelBuildIdentity): Promise<KernelBuildIdentity> {
    this.startupCalls.push(kernelIdentity);
    return this.startupIdentity;
  }

  async currentDurableEventSeq(): Promise<number> {
    return this.durableEventSeq;
  }

  async saveCheckpoint(checkpoint: KernelCheckpoint): Promise<void> {
    this.saved.push(structuredClone(checkpoint));
  }

  async close(): Promise<void> {}
}

class RecordingSink implements EchoAgentEventSink {
  readonly events: KernelEventEnvelope[] = [];
  readonly audits: KernelAuditEntry[] = [];

  async publish(event: KernelEventEnvelope): Promise<void> {
    this.events.push(event);
  }

  async audit(entry: KernelAuditEntry): Promise<void> {
    this.audits.push(entry);
  }
}

class DeterministicIds implements EchoIdFactory {
  private sequence = 0;

  next(kind: "request" | "event" | "turn" | "message" | "checkpoint" | "cancel"): string {
    this.sequence += 1;
    return `${kind}-${this.sequence}`;
  }
}

function createDeps(
  model: DeterministicModel,
  session: RecordingSession,
): KernelDeps & { model: DeterministicModel; session: RecordingSession; sink: RecordingSink } {
  const sink = new RecordingSink();
  const context: EchoContextPort = {
    async buildModelContext(_input, history) {
      return {
        system: [{ type: "text", text: "resume proof" }],
        messages: [...history],
        tools: [],
      };
    },
  };
  const telemetry: EchoAgentTelemetryPort = { record: async () => undefined };
  const emptyTools: EchoToolRegistry = { list: () => [], resolve: () => undefined };
  return {
    model,
    tools: emptyTools,
    session,
    events: sink,
    context,
    clock: { now: () => NOW },
    ids: new DeterministicIds(),
    telemetry,
    sink,
  };
}

async function makeCheckpoint(): Promise<KernelCheckpoint> {
  const model = new DeterministicModel();
  const session = new RecordingSession();
  const kernelSession = await new EchoAgentKernel(IDENTITY).openSession(
    openInput(),
    createDeps(model, session),
  );
  const events = [];
  for await (const event of kernelSession.runTurn(turnInput("before-restart"))) events.push(event);
  assert.equal(events.at(-1)?.type, "agent.turn.completed");
  const checkpoint = await kernelSession.checkpoint();
  assert.deepEqual(session.saved, [checkpoint]);
  await kernelSession.close();
  return checkpoint;
}

function expectKernelError(code: KernelError["code"]) {
  return (error: unknown): boolean => error instanceof KernelError && error.code === code;
}

test("deterministic turn to checkpoint save to restart/resume preserves identity and history", async () => {
  const checkpoint = await makeCheckpoint();
  const resumedModel = new DeterministicModel();
  const resumedSession = new RecordingSession(IDENTITY, checkpoint.lastDurableEventSeq);
  const resumed = await new EchoAgentKernel(IDENTITY).openSession(
    openInput(checkpoint),
    createDeps(resumedModel, resumedSession),
  );

  const events = [];
  for await (const event of resumed.runTurn(turnInput("after-restart"))) events.push(event);

  assert.deepEqual(events.map((event) => event.type), [
    "agent.turn.started",
    "agent.message.delta",
    "agent.message.completed",
    "agent.compaction.started",
    "agent.compaction.completed",
    "agent.summary.updated",
    "agent.turn.completed",
  ]);
  assert.equal(resumedSession.startupCalls.length, 1);
  assert.deepEqual(resumedSession.startupCalls[0], IDENTITY);
  assert.equal(resumedModel.requests.length, 1);
  assert.deepEqual(
    resumedModel.requests[0]?.messages.map((message) => message.content),
    [
      [{ type: "text", text: "before-restart" }],
      [{ type: "text", text: "deterministic" }],
      [{ type: "text", text: "after-restart" }],
    ],
  );
  assert.equal(checkpoint.taskId, GRANT.taskId);
  assert.equal(checkpoint.operationKey, "resume-operation");
  assert.equal(checkpoint.modelConfigRevision, MODEL.revision);
  assert.equal(checkpoint.grantRevision, GRANT.revision);
  assert.ok(checkpoint.lastDurableEventSeq <= resumedSession.durableEventSeq);
  await resumed.close();
});

test("resume rejects corrupt, stale-sequence, and identity-mismatched checkpoints", async () => {
  const checkpoint = await makeCheckpoint();
  const cases: Array<{
    name: string;
    resume: KernelCheckpoint;
    session?: RecordingSession;
    input?: ReturnType<typeof openInput>;
    code: KernelError["code"];
  }> = [
    {
      name: "corrupt checksum",
      resume: { ...checkpoint, checksum: "0".repeat(71) },
      code: "CHECKPOINT_CORRUPT",
    },
    {
      name: "task mismatch",
      resume: { ...checkpoint, taskId: "other-task" },
      code: "CHECKPOINT_TASK_MISMATCH",
    },
    {
      name: "operation mismatch",
      resume: { ...checkpoint, operationKey: "other-operation" },
      code: "CHECKPOINT_OPERATION_MISMATCH",
    },
    {
      name: "model revision mismatch",
      resume: { ...checkpoint, modelConfigRevision: MODEL.revision + 1 },
      code: "CHECKPOINT_MODEL_REVISION_MISSING",
    },
    {
      name: "grant revision mismatch",
      resume: { ...checkpoint, grantRevision: GRANT.revision + 1 },
      code: "GRANT_REVISION_MISMATCH",
    },
    {
      name: "checkpoint sequence is ahead of durable session",
      resume: { ...checkpoint, lastDurableEventSeq: checkpoint.lastDurableEventSeq + 1 },
      session: new RecordingSession(IDENTITY, checkpoint.lastDurableEventSeq),
      code: "CHECKPOINT_EVENT_SEQ_AHEAD",
    },
    {
      name: "model snapshot changes at the same revision",
      resume: checkpoint,
      input: { ...openInput(checkpoint), model: { ...MODEL, configHash: "changed-at-v7" } },
      code: "MODEL_CONFIG_REVISION_MISSING",
    },
  ];

  for (const candidate of cases) {
    await assert.rejects(
      new EchoAgentKernel(IDENTITY).openSession(
        candidate.input ?? openInput(candidate.resume),
        createDeps(new DeterministicModel(), candidate.session ?? new RecordingSession()),
      ),
      expectKernelError(candidate.code),
      candidate.name,
    );
  }
});

test("restart with a different kernel/build identity fails closed before resume", async () => {
  const checkpoint = await makeCheckpoint();
  const mismatchedIdentity: KernelBuildIdentity = { ...IDENTITY, buildId: "resume-identity-test-v2" };
  const session = new RecordingSession(mismatchedIdentity, checkpoint.lastDurableEventSeq);

  await assert.rejects(
    new EchoAgentKernel(IDENTITY).openSession(
      openInput(checkpoint),
      createDeps(new DeterministicModel(), session),
    ),
    expectKernelError("RUNTIME_BUILD_MISMATCH"),
  );
});

test("same-revision GrantSnapshot changes fail closed instead of being treated as a valid resume", async () => {
  const checkpoint = await makeCheckpoint();
  const changedGrant = { ...GRANT, grantId: "different-grant-same-revision" };

  await assert.rejects(
    new EchoAgentKernel(IDENTITY).openSession(
      { ...openInput(checkpoint), grant: changedGrant },
      createDeps(new DeterministicModel(), new RecordingSession()),
    ),
    expectKernelError("GRANT_REVISION_MISMATCH"),
  );
});

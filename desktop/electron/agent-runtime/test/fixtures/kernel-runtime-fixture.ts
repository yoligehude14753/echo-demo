import { threadId } from "node:worker_threads";
import type {
  AgentTurnInput,
  CancelReason,
  KernelCheckpoint,
  KernelEventEnvelope,
} from "../../../../agent-kernel/core/index.ts";
import type { KernelWorkerRuntime, KernelWorkerRuntimeFactoryInput } from "../../worker/bridge.ts";

function event(input: AgentTurnInput, type: KernelEventEnvelope["type"], payload: Record<string, string | number | boolean>): KernelEventEnvelope {
  return {
    schemaVersion: 1,
    taskId: input.taskId,
    operationKey: input.operationKey,
    runtimeEventId: `fixture-runtime-${Math.random().toString(16).slice(2)}`,
    occurredAt: new Date().toISOString(),
    type,
    payload,
  };
}

class FixtureRuntime implements KernelWorkerRuntime {
  private cancelResolver: (() => void) | undefined;
  private cancelled = false;
  private closed = false;

  async *runTurn(input: AgentTurnInput): AsyncIterable<KernelEventEnvelope> {
    if (this.closed) throw new Error("fixture runtime is closed");
    if (input.context.crash === true) {
      process.exit(23);
    }
    yield event(input, "agent.turn.started", { workerPid: process.pid, workerThreadId: threadId });
    if (input.context.waitForCancel === true) {
      await new Promise<void>((resolve) => {
        this.cancelResolver = resolve;
      });
      if (this.cancelled) {
        yield event(input, "agent.turn.cancelled", { cancelReason: "user" });
        return;
      }
    }
    yield event(input, "agent.message.delta", { text: "fixture-response" });
    yield event(input, "agent.message.completed", { text: "fixture-response" });
    yield event(input, "agent.turn.completed", { stopReason: "end_turn" });
  }

  async checkpoint(): Promise<KernelCheckpoint> {
    return {
      schemaVersion: 1,
      checkpointId: "fixture-checkpoint-1",
      taskId: "task-runtime",
      operationKey: "operation-runtime",
      modelConfigRevision: 1,
      grantRevision: 1,
      grantSnapshot: {
        schemaVersion: 1,
        grantId: "grant-runtime",
        revision: 1,
        taskId: "task-runtime",
        deviceId: "device-runtime",
        issuedAt: "2026-07-15T00:00:00.000Z",
        expiresAt: "2099-07-15T00:00:00.000Z",
        workspaceRoots: [],
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
        artifacts: {},
        secrets: {},
        skills: {},
      },
      lastDurableEventSeq: 0,
      messages: [],
      compactState: { schemaVersion: 1, strategy: "none", summaryHash: null, messageCountAtBoundary: 0 },
      budgetState: { turnsUsed: 1, toolCallsUsed: 0, modelInputTokens: 0, modelOutputTokens: 0 },
      createdAt: new Date().toISOString(),
      checksum: "sha256:fixture",
    };
  }

  async cancel(_reason: CancelReason): Promise<void> {
    this.cancelled = true;
    this.cancelResolver?.();
    this.cancelResolver = undefined;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.cancelResolver?.();
    this.cancelResolver = undefined;
  }
}

export async function createWorkerRuntime(_input: KernelWorkerRuntimeFactoryInput): Promise<KernelWorkerRuntime> {
  return new FixtureRuntime();
}

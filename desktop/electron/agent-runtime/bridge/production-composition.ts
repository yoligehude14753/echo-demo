import type {
  AgentTurnInput,
  KernelEventEnvelope,
  OpenSessionInput,
} from "../../../agent-kernel/core/index.ts";
import {
  EmbeddedRuntimePortServer,
  type EmbeddedRuntimeCommandHandler,
} from "./embedded-runtime-server.ts";
import type { RuntimeDuplex } from "./framed-runtime.ts";
import {
  WorkerManager,
  type WorkerRuntimeSession,
} from "../pool/worker-manager.ts";
import type { RuntimeManifest } from "../worker/identity.ts";

export type ProductionCompositionOptions = {
  manifest: RuntimeManifest;
  factoryModule?: URL | string;
  resolveOpenInput: (
    payload: Record<string, unknown>,
  ) => Promise<OpenSessionInput>;
  resolveTurnInput: (
    payload: Record<string, unknown>,
    open: OpenSessionInput,
  ) => Promise<AgentTurnInput>;
  resolveSnapshot?: (
    taskId: string,
    operationKey: string,
  ) => Promise<Record<string, unknown>>;
};

type ActiveRuntime = {
  manager: WorkerManager;
  session: WorkerRuntimeSession;
};

function requireIdentity(
  open: OpenSessionInput,
  taskId: string,
  operationKey: string,
): void {
  if (open.taskId !== taskId || open.operationKey !== operationKey) {
    throw new Error("PRODUCTION_SESSION_IDENTITY_MISMATCH");
  }
}

export function createProductionEmbeddedRuntimeCommandHandler(
  options: ProductionCompositionOptions,
): EmbeddedRuntimeCommandHandler {
  const active = new Map<string, ActiveRuntime>();
  const factoryModule =
    options.factoryModule ?? new URL("./production-factory.ts", import.meta.url);

  return {
    async submit({ taskId, operationKey, payload, emit }) {
      if (active.has(taskId)) throw new Error("PRODUCTION_TASK_ALREADY_ACTIVE");
      const open = await options.resolveOpenInput(payload);
      requireIdentity(open, taskId, operationKey);
      const manager = new WorkerManager({
        manifest: options.manifest,
        factoryModule,
      });
      const session = await manager.open(open);
      active.set(taskId, { manager, session });
      void (async () => {
        try {
          const input = await options.resolveTurnInput(payload, open);
          requireIdentity(open, input.taskId, input.operationKey);
          for await (const event of session.runTurn(input)) {
            emit({ event: event as unknown as Record<string, unknown> });
          }
        } finally {
          active.delete(taskId);
          await session.close();
        }
      })().catch(() => {
        // The backend observes the absence of a terminal event and keeps the
        // durable task fail-closed for recovery; no synthetic success is sent.
      });
      return { taskId, operationKey };
    },
    async cancel({ taskId, operationKey, payload }) {
      const current = active.get(taskId);
      if (!current) return { taskId, operationKey, cancelled: true };
      const reason = payload.reason;
      if (reason !== "user" && reason !== "timeout" && reason !== "provider_error" && reason !== "grant_revoked") {
        throw new Error("PRODUCTION_CANCEL_REASON_INVALID");
      }
      await current.session.cancel(reason);
      return { taskId, operationKey, cancelled: true };
    },
    async snapshot({ taskId, operationKey }) {
      if (!options.resolveSnapshot) {
        throw new Error("PRODUCTION_SESSION_SNAPSHOT_UNBOUND");
      }
      return options.resolveSnapshot(taskId, operationKey);
    },
  };
}

export function createProductionEmbeddedRuntimePort(
  duplex: RuntimeDuplex,
  nonce: string,
  options: ProductionCompositionOptions,
): EmbeddedRuntimePortServer {
  if (!nonce) throw new Error("PRODUCTION_RUNTIME_NONCE_REQUIRED");
  const server = new EmbeddedRuntimePortServer(
    duplex,
    nonce,
    createProductionEmbeddedRuntimeCommandHandler(options),
  );
  server.start();
  return server;
}

export type ProductionRuntimeEvent = KernelEventEnvelope;

import {
  completeSyncOperation,
  failSyncOperation,
  loadSyncState,
  markSyncOperationSending,
  normalizeSyncCursor,
  pendingSyncOperations,
  updateSyncState,
  type SyncOutboxItem,
  type SyncStorage,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncState.ts";
import type { SyncChange } from "./syncProtocol.ts";

export interface SyncPushResult {
  result: "applied" | "duplicate" | "conflict";
  current?: SyncChange | null;
}

export interface SyncChangeResult {
  changes: SyncChange[];
  cursor: string | null;
  reset_required?: boolean;
  snapshot_required?: boolean;
}

export interface SyncClientLike {
  push(item: SyncOutboxItem): Promise<SyncPushResult>;
  changes(cursor: string | null, limit?: number): Promise<SyncChangeResult>;
  snapshot(): Promise<SyncChangeResult>;
}

export type SyncChangeApplier = (change: SyncChange) => void;

export interface SyncWorkerBatchResult {
  attempted: number;
  completed: number;
  duplicates: number;
  conflicts: number;
  received: number;
  used_snapshot: boolean;
}

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message.slice(0, 160);
  return "同步请求失败，请稍后重试";
}

function cursorRequiresSnapshot(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const candidate = error as { status?: unknown; code?: unknown };
  const status = typeof candidate.status === "number" ? candidate.status : null;
  const code = typeof candidate.code === "string" ? candidate.code.toLowerCase() : "";
  return status === 400 || status === 409 || status === 410 || code.includes("cursor");
}

function applyChanges(
  result: SyncChangeResult,
  apply: SyncChangeApplier,
  storage?: SyncStorage,
): number {
  const cursor = normalizeSyncCursor(result.cursor);
  for (const change of result.changes) apply(change);
  updateSyncState(
    (state) => {
      const failedItem = state.outbox.find((item) => item.status === "failed");
      return {
        ...state,
        cursor: cursor ?? state.cursor,
        status: failedItem ? "failed" : "synced",
        last_error: failedItem?.last_error ?? null,
        last_synced_at: new Date().toISOString(),
      };
    },
    storage,
  );
  return result.changes.length;
}

export class SyncWorkerCore {
  private readonly client: SyncClientLike;
  private readonly apply: SyncChangeApplier;
  private readonly storage?: SyncStorage;

  constructor(
    client: SyncClientLike,
    apply: SyncChangeApplier,
    storage?: SyncStorage,
  ) {
    this.client = client;
    this.apply = apply;
    this.storage = storage;
  }

  async pushBatch(limit = 20): Promise<SyncWorkerBatchResult> {
    const state = loadSyncState(this.storage);
    const items = state.sync_token ? pendingSyncOperations(limit, this.storage) : [];
    const result: SyncWorkerBatchResult = {
      attempted: items.length,
      completed: 0,
      duplicates: 0,
      conflicts: 0,
      received: 0,
      used_snapshot: false,
    };
    for (const item of items) {
      markSyncOperationSending(item.operation_id, this.storage);
      try {
        const response = await this.client.push(item);
        if (response.result === "conflict") {
          result.conflicts += 1;
          if (!response.current) {
            throw new Error("同步冲突响应缺少服务端当前值");
          }
          this.apply(response.current);
        } else if (response.result === "duplicate") {
          result.duplicates += 1;
        } else if (response.result !== "applied") {
          throw new Error("同步服务响应格式无效");
        }
        completeSyncOperation(item.operation_id, this.storage);
        result.completed += 1;
      } catch (error) {
        failSyncOperation(item.operation_id, errorText(error), this.storage);
      }
    }
    return result;
  }

  async receiveChanges(forceSnapshot = false, limit = 100): Promise<SyncWorkerBatchResult> {
    const state = loadSyncState(this.storage);
    if (!state.sync_token) {
      return {
        attempted: 0,
        completed: 0,
        duplicates: 0,
        conflicts: 0,
        received: 0,
        used_snapshot: false,
      };
    }
    let usedSnapshot = forceSnapshot;
    try {
      let response: SyncChangeResult;
      if (forceSnapshot) {
        response = await this.client.snapshot();
      } else {
        try {
          response = await this.client.changes(state.cursor, limit);
          if (response.reset_required || response.snapshot_required) {
            response = await this.client.snapshot();
            usedSnapshot = true;
          }
        } catch (error) {
          if (!cursorRequiresSnapshot(error)) throw error;
          response = await this.client.snapshot();
          usedSnapshot = true;
        }
      }
      const received = applyChanges(response, this.apply, this.storage);
      return {
        attempted: 0,
        completed: 0,
        duplicates: 0,
        conflicts: 0,
        received,
        used_snapshot: usedSnapshot,
      };
    } catch (error) {
      updateSyncState(
        (current) => ({ ...current, status: "failed", last_error: errorText(error) }),
        this.storage,
      );
      throw error;
    }
  }

  async reconcile(limit = 20): Promise<SyncWorkerBatchResult> {
    const pushed = await this.pushBatch(limit);
    const received = await this.receiveChanges();
    return {
      ...pushed,
      received: received.received,
      used_snapshot: received.used_snapshot,
    };
  }
}

import {
  completeSyncOperation,
  failSyncOperation,
  isSyncHistoryManifestCovered,
  isSyncStateReady,
  loadSyncState,
  markSyncOperationSending,
  normalizeSyncCursor,
  pendingSyncOperations,
  rememberSyncEntityRevisions,
  updateSyncState,
  type SyncHistoryManifest,
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
  manifest?: SyncHistoryManifest | null;
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
  replayed_from_zero: boolean;
}

const MAX_CHANGE_PAGE_LIMIT = 20;
const MAX_CHANGE_PAGES_PER_RECONCILE = 128;

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message.slice(0, 160);
  return "同步请求失败，请稍后重试";
}

function cursorRequiresReplay(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const candidate = error as { status?: unknown; code?: unknown };
  const status = typeof candidate.status === "number" ? candidate.status : null;
  const code = typeof candidate.code === "string" ? candidate.code.toLowerCase() : "";
  return status === 400 || status === 409 || status === 410 || code.includes("cursor");
}

function cursorRegressed(previous: string | null, next: string | null): boolean {
  if (!previous || !next || !/^\d+$/.test(previous) || !/^\d+$/.test(next)) return false;
  return BigInt(next) < BigInt(previous);
}

function restartHistoryPagination(storage?: SyncStorage): void {
  updateSyncState(
    (state) => ({
      ...state,
      cursor: "0",
      status: state.sync_token ? "syncing" : "unpaired",
      last_error: null,
      snapshot_complete: false,
      history_manifest: null,
      canonical_revisions: {},
    }),
    storage,
  );
}

function applyChanges(
  result: SyncChangeResult,
  apply: SyncChangeApplier,
  storage?: SyncStorage,
): number {
  const cursor = normalizeSyncCursor(result.cursor);
  for (const change of result.changes) apply(change);
  rememberSyncEntityRevisions(result.changes, storage);
  updateSyncState(
    (state) => {
      const failedItem = state.outbox.find((item) => item.status === "failed");
      const progress = {
        ...state,
        cursor: cursor ?? state.cursor,
        last_synced_at: new Date().toISOString(),
        history_manifest: result.manifest ?? state.history_manifest,
      };
      const completed = {
        ...progress,
        // Completion is evidence about this exact manifest/cursor pair, not a
        // sticky flag.  A later server cursor must fail closed until every
        // manifest entity is present locally again.
        snapshot_complete: isSyncHistoryManifestCovered(progress),
      };
      return {
        ...completed,
        status: failedItem
          ? "failed"
          : isSyncStateReady(completed)
            ? "synced"
            : "syncing",
        last_error: failedItem?.last_error ?? null,
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
      replayed_from_zero: false,
    };
    for (const item of items) {
      markSyncOperationSending(item.operation_id, this.storage);
      try {
        const response = await this.client.push(item);
        if (response.result === "conflict") {
          result.conflicts += 1;
          if (!response.current) {
            failSyncOperation(
              item.operation_id,
              "同步冲突响应缺少服务端当前值",
              this.storage,
              { retryable: false },
            );
            continue;
          }
          this.apply(response.current);
          rememberSyncEntityRevisions([response.current], this.storage);
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

  async receiveChanges(forceReplayFromZero = false, limit = MAX_CHANGE_PAGE_LIMIT): Promise<SyncWorkerBatchResult> {
    let state = loadSyncState(this.storage);
    if (!state.sync_token) {
      return {
        attempted: 0,
        completed: 0,
        duplicates: 0,
        conflicts: 0,
        received: 0,
        replayed_from_zero: false,
      };
    }
    // A full snapshot can exceed the desktop transport body limit.  Recovery
    // therefore replays bounded /changes pages from zero instead of issuing
    // one unbounded snapshot request.
    let replayedFromZero = forceReplayFromZero;
    let resetAttempted = forceReplayFromZero;
    if (forceReplayFromZero) {
      restartHistoryPagination(this.storage);
      state = loadSyncState(this.storage);
    }
    try {
      const pageLimit = Math.min(MAX_CHANGE_PAGE_LIMIT, Math.max(1, Math.floor(limit)));
      let cursor = state.cursor;
      let received = 0;
      for (let page = 0; page < MAX_CHANGE_PAGES_PER_RECONCILE; page += 1) {
        let response: SyncChangeResult | null = null;
        try {
          response = await this.client.changes(cursor, pageLimit);
          const responseCursor = normalizeSyncCursor(response.cursor);
          if (
            response.reset_required ||
            response.snapshot_required ||
            cursorRegressed(cursor, responseCursor)
          ) {
            if (resetAttempted) {
              throw new Error("同步服务重复要求全量历史恢复");
            }
            restartHistoryPagination(this.storage);
            cursor = "0";
            replayedFromZero = true;
            resetAttempted = true;
            continue;
          }
        } catch (error) {
          if (!cursorRequiresReplay(error) || resetAttempted) throw error;
          restartHistoryPagination(this.storage);
          cursor = "0";
          replayedFromZero = true;
          resetAttempted = true;
          continue;
        }

        if (!response) continue;
        const nextCursor = normalizeSyncCursor(response.cursor) ?? cursor;
        const progressed = nextCursor !== cursor;
        const reachedManifest = response.manifest?.cursor === nextCursor;
        received += applyChanges(response, this.apply, this.storage);
        cursor = nextCursor;
        if (reachedManifest || response.changes.length < pageLimit || !progressed) break;
      }
      return {
        attempted: 0,
        completed: 0,
        duplicates: 0,
        conflicts: 0,
        received,
        replayed_from_zero: replayedFromZero,
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
      replayed_from_zero: received.replayed_from_zero,
    };
  }
}

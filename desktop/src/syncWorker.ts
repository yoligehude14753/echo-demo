import { useEffect } from "react";
import {
  configuredSyncHubBase,
  SYNC_HUB_BASE_EVENT,
} from "@/runtime";
import { SyncHubClient, type SyncChange } from "@/syncApi";
import {
  parseSyncFrame,
  syncHubWebSocketUrl,
} from "@/syncWorkerProtocol";
import {
  SyncWorkerCore,
  type SyncClientLike,
} from "@/syncWorkerCore";
import {
  isSyncStateReady,
  loadSyncState,
  rememberSyncEntityRevisions,
  SYNC_STATE_EVENT,
  updateSyncState,
  type SyncState,
  type SyncStorage,
} from "@/syncState";
import {
  advanceCompletedLegacyHistoryPage,
  refillLegacyHistoryPage,
} from "@/legacyHistorySync";
import { useStore } from "@/store";

const SYNC_WORKER_POLL_MS = 15_000;
// 历史导入仍在进行时不等待常规后台轮询；每轮最多推送 8 个小页，
// 因而这个短间隔既保持 renderer/outbox 有界，也不会把“本页完成”误报成全量完成。
const LEGACY_HISTORY_CONTINUATION_MS = 1_000;
const SYNC_RECONNECT_BASE_MS = 1_000;
const SYNC_RECONNECT_MAX_MS = 30_000;

interface SyncSocketLike {
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface SyncWorkerOptions {
  client?: SyncClientLike;
  storage?: SyncStorage;
  socketFactory?: (url: string) => SyncSocketLike;
  applyChange?: (change: SyncChange) => void;
}

function defaultSocketFactory(url: string): SyncSocketLike {
  return new WebSocket(url);
}

export function syncWorkerPollDelay(
  state: Pick<SyncState, "status" | "legacy_history">,
): number {
  const legacy = state.legacy_history;
  if (state.status !== "failed" && legacy && legacy.phase !== "complete") {
    return LEGACY_HISTORY_CONTINUATION_MS;
  }
  return SYNC_WORKER_POLL_MS;
}

export class SyncWorkerController {
  private readonly core: SyncWorkerCore;
  private readonly storage?: SyncStorage;
  private readonly socketFactory: (url: string) => SyncSocketLike;
  private readonly applyChange: (change: SyncChange) => void;
  private active = false;
  private socket: SyncSocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private retryCount = 0;
  private reconcilePromise: Promise<void> | null = null;

  constructor(options: SyncWorkerOptions = {}) {
    this.storage = options.storage;
    this.socketFactory = options.socketFactory ?? defaultSocketFactory;
    this.applyChange = options.applyChange ?? ((change) => {
      useStore.getState().applyRemoteSyncEntity(
        change.entity_type,
        change.payload,
        change.revision,
      );
    });
    this.core = new SyncWorkerCore(
      options.client ?? new SyncHubClient(),
      this.applyChange,
      this.storage,
    );
  }

  start(): void {
    if (this.active) return;
    this.active = true;
    this.retryCount = 0;
    void this.reconcile();
  }

  stop(): void {
    this.active = false;
    this.clearTimers();
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000, "sync worker stopped");
  }

  restart(): void {
    this.stop();
    this.start();
  }

  onStateChanged(): void {
    if (!this.active) return;
    if (!loadSyncState(this.storage).sync_token) {
      this.closeSocket();
      return;
    }
    void this.reconcile();
    this.connectSocket();
  }

  private async reconcile(): Promise<void> {
    if (!this.active || this.reconcilePromise) return this.reconcilePromise ?? Promise.resolve();
    this.reconcilePromise = (async () => {
      try {
        // Legacy desktop history is streamed in small, durable outbox pages.
        // This keeps the renderer storage bounded while every entity still
        // traverses the normal authenticated sync push path.
        for (let page = 0; page < 8 && this.active; page += 1) {
          advanceCompletedLegacyHistoryPage(this.storage);
          const queued = await refillLegacyHistoryPage(this.storage);
          const pushed = await this.core.pushBatch(20);
          advanceCompletedLegacyHistoryPage(this.storage);
          if (
            pushed.attempted > 0 &&
            pushed.completed === 0 &&
            pushed.duplicates === 0 &&
            pushed.conflicts === 0
          ) {
            break;
          }
          if (queued === 0 && pushed.attempted === 0) break;
        }
        await this.core.receiveChanges();
      } catch {
        // core 已把错误写入同步状态；下一次 poll/重连继续尝试。
      } finally {
        this.reconcilePromise = null;
        if (this.active) {
          const state = loadSyncState(this.storage);
          const delay = syncWorkerPollDelay(state);
          if (delay === LEGACY_HISTORY_CONTINUATION_MS && state.status !== "syncing") {
            updateSyncState((current) => ({ ...current, status: "syncing" }), this.storage);
          }
          this.schedulePoll(delay);
          this.connectSocket();
        }
      }
    })();
    return this.reconcilePromise;
  }

  private clearTimers(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.reconnectTimer = null;
    this.pollTimer = null;
  }

  private schedulePoll(delay = SYNC_WORKER_POLL_MS): void {
    if (!this.active) return;
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.reconcile();
    }, delay);
  }

  private scheduleReconnect(): void {
    if (
      !this.active ||
      !configuredSyncHubBase() ||
      !loadSyncState(this.storage).sync_token ||
      this.reconnectTimer
    ) return;
    this.retryCount = Math.min(this.retryCount + 1, 8);
    const delay = Math.min(
      SYNC_RECONNECT_BASE_MS * 2 ** (this.retryCount - 1),
      SYNC_RECONNECT_MAX_MS,
    );
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.core.receiveChanges().catch(() => undefined);
      this.connectSocket();
    }, delay);
  }

  private closeSocket(): void {
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000, "sync pairing unavailable");
  }

  private connectSocket(): void {
    const hubBase = configuredSyncHubBase();
    if (!this.active || this.socket || !hubBase || !loadSyncState(this.storage).sync_token) return;
    const state = loadSyncState(this.storage);
    const url = syncHubWebSocketUrl(hubBase, state.cursor);
    let socket: SyncSocketLike;
    try {
      socket = this.socketFactory(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.retryCount = 0;
      const current = loadSyncState(this.storage);
      try {
        socket.send(
          JSON.stringify({
            type: "client_hello",
            cursor: current.cursor,
            auth: current.sync_token
              ? { type: "bearer", token: current.sync_token }
              : undefined,
          }),
        );
      } catch {
        socket.close(1011, "sync hello failed");
      }
    };
    socket.onmessage = (event) => this.handleSocketMessage(socket, event.data);
    socket.onerror = () => undefined;
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.scheduleReconnect();
    };
  }

  private handleSocketMessage(socket: SyncSocketLike, raw: unknown): void {
    if (this.socket !== socket) return;
    const frame = parseSyncFrame(raw);
    if (!frame) {
      socket.close(1003, "invalid sync frame");
      return;
    }
    if (frame.ping) {
      try {
        socket.send(JSON.stringify({ type: "client_pong" }));
      } catch {
        socket.close(1011, "sync pong failed");
      }
      return;
    }
    if (frame.snapshotRequired) {
      void this.core.receiveChanges(true).catch(() => undefined);
      return;
    }
    if (frame.change) {
      this.applyChange(frame.change);
      rememberSyncEntityRevisions([frame.change], this.storage);
    }
    if (frame.cursor) {
      updateSyncState((state) => {
        const failedItem = state.outbox.find((item) => item.status === "failed");
        const progress = {
          ...state,
          cursor: frame.cursor ?? state.cursor,
          last_synced_at: new Date().toISOString(),
        };
        return {
          ...progress,
          status: failedItem
            ? "failed"
            : isSyncStateReady(progress)
              ? "synced"
              : "syncing",
          last_error: failedItem?.last_error ?? null,
        };
      }, this.storage);
    }
  }
}

export function useSyncWorker(): void {
  useEffect(() => {
    const worker = new SyncWorkerController();
    const onStateChanged = () => worker.onStateChanged();
    const onHubChanged = () => worker.restart();
    window.addEventListener(SYNC_STATE_EVENT, onStateChanged);
    window.addEventListener(SYNC_HUB_BASE_EVENT, onHubChanged);
    worker.start();
    return () => {
      window.removeEventListener(SYNC_STATE_EVENT, onStateChanged);
      window.removeEventListener(SYNC_HUB_BASE_EVENT, onHubChanged);
      worker.stop();
    };
  }, []);
}

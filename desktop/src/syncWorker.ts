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
  loadSyncState,
  SYNC_STATE_EVENT,
  updateSyncState,
  type SyncStorage,
} from "@/syncState";
import { useStore } from "@/store";

const SYNC_WORKER_POLL_MS = 15_000;
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
      useStore.getState().applyRemoteSyncEntity(change.entity_type, change.payload);
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
        await this.core.reconcile();
      } catch {
        // core 已把错误写入同步状态；下一次 poll/重连继续尝试。
      } finally {
        this.reconcilePromise = null;
        if (this.active) {
          this.schedulePoll();
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

  private schedulePoll(): void {
    if (!this.active) return;
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.reconcile();
    }, SYNC_WORKER_POLL_MS);
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
    if (frame.change) this.applyChange(frame.change);
    if (frame.cursor) {
      updateSyncState((state) => {
        const failedItem = state.outbox.find((item) => item.status === "failed");
        return {
          ...state,
          cursor: frame.cursor ?? state.cursor,
          status: failedItem ? "failed" : "synced",
          last_error: failedItem?.last_error ?? null,
          last_synced_at: new Date().toISOString(),
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

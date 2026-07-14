export type SyncEntityType =
  | "transcript_segment"
  | "meeting_summary"
  | "memory";

export type SyncOutboxStatus = "pending" | "sending" | "failed";
export type SyncStatus = "unpaired" | "syncing" | "synced" | "failed";

export interface SyncOutboxItem {
  operation_id: string;
  device_id: string;
  entity_type: SyncEntityType;
  entity_id: string;
  base_revision: number;
  updated_at: string;
  payload: Record<string, unknown>;
  status: SyncOutboxStatus;
  retry_count: number;
  last_error: string | null;
}

export interface SyncState {
  schema: 1;
  device_id: string;
  device_name: string;
  platform: "android" | "web";
  sync_token: string | null;
  cursor: string | null;
  status: SyncStatus;
  last_error: string | null;
  last_synced_at: string | null;
  outbox: SyncOutboxItem[];
}

export interface SyncStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export const SYNC_STATE_KEY = "echodesk.syncState.v1";
export const SYNC_STATE_EVENT = "echodesk:sync-state-change";
export const SYNC_MEMORY_EVENT = "echodesk:sync-memory-change";
export const SYNC_SCHEMA = 1;

function browserStorage(): SyncStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function randomDeviceId(): string {
  const value = globalThis.crypto?.randomUUID?.();
  if (value) return `device-${value}`;
  const bytes = new Uint8Array(16);
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error("安全随机数不可用，无法创建设备身份");
  }
  globalThis.crypto.getRandomValues(bytes);
  return `device-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function platformName(): "android" | "web" {
  return typeof window !== "undefined" && window.Capacitor?.isNativePlatform?.()
    ? "android"
    : "web";
}

function defaultState(): SyncState {
  return {
    schema: SYNC_SCHEMA,
    device_id: randomDeviceId(),
    device_name: platformName() === "android" ? "EchoDesk Android" : "EchoDesk",
    platform: platformName(),
    sync_token: null,
    cursor: null,
    status: "unpaired",
    last_error: null,
    last_synced_at: null,
    outbox: [],
  };
}

function validEntityType(value: unknown): value is SyncEntityType {
  return (
    value === "transcript_segment" || value === "meeting_summary" || value === "memory"
  );
}

function normalizeOutboxItem(value: unknown, deviceId: string): SyncOutboxItem | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Partial<SyncOutboxItem>;
  if (
    typeof item.operation_id !== "string" ||
    item.operation_id.startsWith("capture:") ||
    typeof item.entity_id !== "string" ||
    !validEntityType(item.entity_type) ||
    typeof item.payload !== "object" ||
    item.payload === null
  ) {
    return null;
  }
  return {
    operation_id: item.operation_id,
    device_id: typeof item.device_id === "string" ? item.device_id : deviceId,
    entity_type: item.entity_type,
    entity_id: item.entity_id,
    base_revision:
      typeof item.base_revision === "number" && Number.isSafeInteger(item.base_revision)
        ? Math.max(0, item.base_revision)
        : 0,
    updated_at: typeof item.updated_at === "string" ? item.updated_at : new Date(0).toISOString(),
    payload: item.payload as Record<string, unknown>,
    status: item.status === "failed" ? "failed" : "pending",
    retry_count:
      typeof item.retry_count === "number" && Number.isSafeInteger(item.retry_count)
        ? Math.max(0, item.retry_count)
        : 0,
    last_error: typeof item.last_error === "string" ? item.last_error : null,
  };
}

function normalizeState(value: unknown): SyncState | null {
  if (!value || typeof value !== "object") return null;
  const parsed = value as Partial<SyncState>;
  if (parsed.schema !== SYNC_SCHEMA || typeof parsed.device_id !== "string") return null;
  const deviceId = parsed.device_id;
  const outbox = Array.isArray(parsed.outbox)
    ? parsed.outbox
        .map((item) => normalizeOutboxItem(item, deviceId))
        .filter((item): item is SyncOutboxItem => item !== null)
    : [];
  return {
    schema: SYNC_SCHEMA,
    device_id: deviceId,
    device_name: typeof parsed.device_name === "string" ? parsed.device_name : "EchoDesk",
    platform: parsed.platform === "android" ? "android" : "web",
    sync_token: typeof parsed.sync_token === "string" ? parsed.sync_token : null,
    cursor: typeof parsed.cursor === "string" ? parsed.cursor : null,
    status:
      parsed.status === "syncing" || parsed.status === "synced" || parsed.status === "failed"
        ? parsed.status
        : "unpaired",
    last_error: typeof parsed.last_error === "string" ? parsed.last_error : null,
    last_synced_at: typeof parsed.last_synced_at === "string" ? parsed.last_synced_at : null,
    outbox,
  };
}

export function loadSyncState(storage: SyncStorage | null = browserStorage()): SyncState {
  if (!storage) return defaultState();
  try {
    const parsed = normalizeState(JSON.parse(storage.getItem(SYNC_STATE_KEY) ?? "null"));
    if (parsed) return parsed;
  } catch {
    // 损坏的同步 sidecar 不能阻塞本地会议数据；下面会创建新的同步状态。
  }
  const fresh = defaultState();
  saveSyncState(fresh, storage);
  return fresh;
}

export function saveSyncState(state: SyncState, storage: SyncStorage | null = browserStorage()): SyncState {
  if (!storage) return state;
  try {
    storage.setItem(SYNC_STATE_KEY, JSON.stringify(state));
    if (typeof window !== "undefined" && storage === window.localStorage) {
      window.dispatchEvent(new CustomEvent(SYNC_STATE_EVENT, { detail: state }));
    }
  } catch {
    // WebView 存储不可用时保留内存状态；同步 worker 会在下次启动重新配对。
  }
  return state;
}

export function updateSyncState(
  update: (state: SyncState) => SyncState,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return saveSyncState(update(loadSyncState(storage)), storage);
}

export function ensureSyncDeviceId(storage: SyncStorage | null = browserStorage()): string {
  return loadSyncState(storage).device_id;
}

export function setPairingState(
  pairing: {
    device_id?: string;
    sync_token: string;
    cursor: string | null;
    device_name?: string;
    platform?: "android" | "web";
  },
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      device_id: pairing.device_id ?? state.device_id,
      sync_token: pairing.sync_token,
      cursor: pairing.cursor,
      device_name: pairing.device_name ?? state.device_name,
      platform: pairing.platform ?? state.platform,
      status: "synced",
      last_error: null,
      outbox: state.outbox.map((item) =>
        item.device_id === state.device_id && pairing.device_id
          ? { ...item, device_id: pairing.device_id }
          : item,
      ),
    }),
    storage,
  );
}

export function clearPairing(storage: SyncStorage | null = browserStorage()): SyncState {
  return updateSyncState(
    (state) => ({ ...state, sync_token: null, cursor: null, status: "unpaired", last_error: null }),
    storage,
  );
}

export function enqueueSyncOperation(
  item: Omit<SyncOutboxItem, "status" | "retry_count" | "last_error">,
  storage: SyncStorage | null = browserStorage(),
): SyncOutboxItem {
  if (item.operation_id.startsWith("capture:")) {
    throw new Error("capture 幂等键不能写入 sync outbox");
  }
  let result = item as SyncOutboxItem;
  updateSyncState((state) => {
    const existing = state.outbox.find((entry) => entry.operation_id === item.operation_id);
    if (existing) {
      result = existing;
      return state;
    }
    result = { ...item, status: "pending", retry_count: 0, last_error: null };
    return { ...state, outbox: [...state.outbox, result] };
  }, storage);
  return result;
}

export function pendingSyncOperations(
  limit = 20,
  storage: SyncStorage | null = browserStorage(),
): SyncOutboxItem[] {
  return loadSyncState(storage).outbox
    .filter((item) => item.status === "pending" || item.status === "failed")
    .slice(0, Math.max(0, limit));
}

export function markSyncOperationSending(
  operationId: string,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      status: "syncing",
      outbox: state.outbox.map((item) =>
        item.operation_id === operationId ? { ...item, status: "sending" } : item,
      ),
    }),
    storage,
  );
}

export function completeSyncOperation(
  operationId: string,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      outbox: state.outbox.filter((item) => item.operation_id !== operationId),
      status: "synced",
      last_error: null,
      last_synced_at: new Date().toISOString(),
    }),
    storage,
  );
}

export function failSyncOperation(
  operationId: string,
  error: string,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      status: "failed",
      last_error: error,
      outbox: state.outbox.map((item) =>
        item.operation_id === operationId
          ? { ...item, status: "failed", retry_count: item.retry_count + 1, last_error: error }
          : item,
      ),
    }),
    storage,
  );
}

export function removeSyncOperation(
  operationId: string,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      outbox: state.outbox.filter((item) => item.operation_id !== operationId),
    }),
    storage,
  );
}

export function makeOperationId(entityType: SyncEntityType, entityId: string): string {
  return `${entityType}:${entityId}:${randomDeviceId().slice("device-".length)}`;
}

export function resetSyncStateForTest(storage: SyncStorage): void {
  storage.removeItem?.(SYNC_STATE_KEY);
}

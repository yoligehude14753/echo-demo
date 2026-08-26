export type SyncEntityType =
  | "meeting"
  | "transcript_segment"
  | "meeting_summary"
  | "artifact"
  | "memory";

export const SYNC_HISTORY_ENTITY_TYPES = [
  "meeting",
  "transcript_segment",
  "meeting_summary",
  "artifact",
] as const;
export type SyncHistoryEntityType = (typeof SYNC_HISTORY_ENTITY_TYPES)[number];

export interface SyncHistoryManifest {
  cursor: string;
  entity_counts: Record<SyncHistoryEntityType, number>;
}

export type LegacyHistorySyncPhase =
  | "meetings"
  | "segments"
  | "summaries"
  | "artifacts"
  | "complete";

export interface LegacyHistorySyncState {
  schema: 1;
  fingerprint: string;
  phase: LegacyHistorySyncPhase;
  offset: number;
  page_end_offset: number | null;
  page_done: boolean;
  meeting_count: number;
  segment_count: number;
  summary_count: number;
  artifact_count: number;
}

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
  legacy_page?: {
    fingerprint: string;
    phase: Exclude<LegacyHistorySyncPhase, "complete">;
    offset: number;
  };
  status: SyncOutboxStatus;
  retry_count: number;
  last_error: string | null;
  retryable?: boolean;
}

export interface SyncState {
  schema: 2;
  device_id: string;
  device_name: string;
  platform: "android" | "web";
  sync_token: string | null;
  cursor: string | null;
  status: SyncStatus;
  last_error: string | null;
  last_synced_at: string | null;
  outbox: SyncOutboxItem[];
  canonical_revisions: Record<string, number>;
  snapshot_complete: boolean;
  history_manifest: SyncHistoryManifest | null;
  legacy_scan_complete: boolean;
  legacy_history: LegacyHistorySyncState | null;
}

export interface SyncStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export const SYNC_STATE_KEY = "echodesk.syncState.v1";
export const SYNC_STATE_EVENT = "echodesk:sync-state-change";
export const SYNC_MEMORY_EVENT = "echodesk:sync-memory-change";
export const SYNC_SCHEMA = 2;

export function normalizeSyncCursor(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error("同步 cursor 必须是非负整数或非空字符串");
    }
    return String(value);
  }
  if (typeof value === "string") {
    const cursor = value.trim();
    if (!cursor) throw new Error("同步 cursor 不能为空");
    return cursor;
  }
  throw new Error("同步 cursor 类型无效");
}

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

function legacyScanCompleteByDefault(): boolean {
  return (
    typeof window === "undefined" ||
    typeof window.echo?.loadLocalLegacyHistory !== "function"
  );
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
    canonical_revisions: {},
    snapshot_complete: false,
    history_manifest: null,
    legacy_scan_complete: legacyScanCompleteByDefault(),
    legacy_history: null,
  };
}

function validEntityType(value: unknown): value is SyncEntityType {
  return (
    value === "meeting" ||
    value === "transcript_segment" ||
    value === "meeting_summary" ||
    value === "artifact" ||
    value === "memory"
  );
}

function normalizeLegacyHistoryState(value: unknown): LegacyHistorySyncState | null {
  if (!value || typeof value !== "object") return null;
  const parsed = value as Partial<LegacyHistorySyncState>;
  const phase = parsed.phase;
  if (
    typeof parsed.fingerprint !== "string" ||
    (phase !== "meetings" &&
      phase !== "segments" &&
      phase !== "summaries" &&
      phase !== "artifacts" &&
      phase !== "complete")
  ) {
    return null;
  }
  const nonNegativeInt = (candidate: unknown, fallback: number): number =>
    typeof candidate === "number" && Number.isSafeInteger(candidate) && candidate >= 0
      ? candidate
      : fallback;
  const pageEnd =
    parsed.page_end_offset === null || parsed.page_end_offset === undefined
      ? null
      : nonNegativeInt(parsed.page_end_offset, 0);
  return {
    schema: 1,
    fingerprint: parsed.fingerprint,
    phase,
    offset: nonNegativeInt(parsed.offset, 0),
    page_end_offset: pageEnd,
    page_done: parsed.page_done === true,
    meeting_count: nonNegativeInt(parsed.meeting_count, 0),
    segment_count: nonNegativeInt(parsed.segment_count, 0),
    summary_count: nonNegativeInt(parsed.summary_count, 0),
    artifact_count: nonNegativeInt(parsed.artifact_count, 0),
  };
}

function normalizeStoredCursor(value: unknown): string | null {
  try {
    return normalizeSyncCursor(value);
  } catch {
    return null;
  }
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
    retryable: item.retryable !== false,
    legacy_page:
      item.legacy_page &&
      typeof item.legacy_page.fingerprint === "string" &&
      (item.legacy_page.phase === "meetings" ||
        item.legacy_page.phase === "segments" ||
        item.legacy_page.phase === "summaries" ||
        item.legacy_page.phase === "artifacts") &&
      Number.isSafeInteger(item.legacy_page.offset) &&
      item.legacy_page.offset >= 0
        ? {
            fingerprint: item.legacy_page.fingerprint,
            phase: item.legacy_page.phase as Exclude<LegacyHistorySyncPhase, "complete">,
            offset: item.legacy_page.offset,
          }
        : undefined,
  };
}

function normalizeCanonicalRevisions(value: unknown): Record<string, number> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const revisions: Record<string, number> = {};
  for (const [key, revision] of Object.entries(value)) {
    if (Number.isSafeInteger(revision) && revision >= 0) {
      revisions[key] = revision;
    }
  }
  return revisions;
}

function normalizeHistoryManifest(value: unknown): SyncHistoryManifest | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const parsed = value as Partial<SyncHistoryManifest>;
  const cursor = normalizeStoredCursor(parsed.cursor);
  if (cursor === null || !parsed.entity_counts || typeof parsed.entity_counts !== "object") {
    return null;
  }
  const entityCounts = {} as Record<SyncHistoryEntityType, number>;
  for (const entityType of SYNC_HISTORY_ENTITY_TYPES) {
    const count = parsed.entity_counts[entityType];
    if (!Number.isSafeInteger(count) || count < 0) return null;
    entityCounts[entityType] = count;
  }
  return { cursor, entity_counts: entityCounts };
}

function normalizeState(value: unknown): SyncState | null {
  if (!value || typeof value !== "object") return null;
  const parsed = value as Partial<SyncState>;
  const storedSchema = (value as { schema?: unknown }).schema;
  if ((storedSchema !== 1 && storedSchema !== SYNC_SCHEMA) || typeof parsed.device_id !== "string") {
    return null;
  }
  const deviceId = parsed.device_id;
  const syncToken = typeof parsed.sync_token === "string" ? parsed.sync_token : null;
  const historyManifest = normalizeHistoryManifest(parsed.history_manifest);
  const legacyHistory = normalizeLegacyHistoryState(parsed.legacy_history);
  const pairedNeedsReplay = Boolean(syncToken && storedSchema !== SYNC_SCHEMA);
  const pairedHistoryIncomplete = Boolean(
    syncToken && (parsed.snapshot_complete !== true || historyManifest === null),
  );
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
    sync_token: syncToken,
    cursor: pairedNeedsReplay ? "0" : normalizeStoredCursor(parsed.cursor),
    status:
      (pairedNeedsReplay || pairedHistoryIncomplete) && parsed.status !== "failed"
        ? "syncing"
        : parsed.status === "syncing" || parsed.status === "synced" || parsed.status === "failed"
          ? parsed.status
          : "unpaired",
    last_error: typeof parsed.last_error === "string" ? parsed.last_error : null,
    last_synced_at: typeof parsed.last_synced_at === "string" ? parsed.last_synced_at : null,
    outbox,
    canonical_revisions: pairedNeedsReplay
      ? {}
      : normalizeCanonicalRevisions(parsed.canonical_revisions),
    snapshot_complete:
      !pairedNeedsReplay && parsed.snapshot_complete === true && historyManifest !== null,
    history_manifest: pairedNeedsReplay ? null : historyManifest,
    legacy_scan_complete:
      parsed.legacy_scan_complete === true ||
      legacyScanCompleteByDefault() ||
      legacyHistory?.phase === "complete",
    legacy_history: legacyHistory,
  };
}

export function loadSyncState(storage: SyncStorage | null = browserStorage()): SyncState {
  if (!storage) return defaultState();
  try {
    const raw = storage.getItem(SYNC_STATE_KEY) ?? "null";
    const parsed = normalizeState(JSON.parse(raw));
    if (parsed) {
      const normalized = JSON.stringify(parsed);
      if (normalized !== raw) storage.setItem(SYNC_STATE_KEY, normalized);
      return parsed;
    }
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

export function isSyncHistoryManifestCovered(
  state: Pick<SyncState, "cursor" | "canonical_revisions" | "history_manifest">,
): boolean {
  const manifest = state.history_manifest;
  if (!manifest || state.cursor !== manifest.cursor) return false;
  return SYNC_HISTORY_ENTITY_TYPES.every((entityType) => {
    const prefix = `${entityType}:`;
    const count = Object.keys(state.canonical_revisions).filter((key) =>
      key.startsWith(prefix),
    ).length;
    return count === manifest.entity_counts[entityType];
  });
}

export function isSyncStateReady(state: SyncState): boolean {
  const legacyComplete = !state.legacy_history || state.legacy_history.phase === "complete";
  return Boolean(
    state.sync_token &&
      state.snapshot_complete &&
      isSyncHistoryManifestCovered(state) &&
      state.legacy_scan_complete &&
      legacyComplete &&
      state.outbox.length === 0 &&
      !state.outbox.some((item) => item.status === "failed"),
  );
}

export function projectSyncStatus(state: SyncState, hostPaired: boolean): SyncStatus {
  if (state.status === "failed") return "failed";
  if (!state.sync_token) return "unpaired";
  if (state.status === "syncing" || !isSyncStateReady(state)) return "syncing";
  return hostPaired || state.status === "synced" ? "synced" : "syncing";
}

function settledSyncStatus(state: SyncState): SyncStatus {
  if (!state.sync_token) return "unpaired";
  if (state.outbox.some((item) => item.status === "failed")) return "failed";
  return isSyncStateReady(state) ? "synced" : "syncing";
}

export function setPairingState(
  pairing: {
    device_id?: string;
    sync_token: string;
    cursor: string | number | null;
    device_name?: string;
    platform?: "android" | "web";
  },
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  normalizeSyncCursor(pairing.cursor);
  return updateSyncState(
    (state) => ({
      ...state,
      device_id: pairing.device_id ?? state.device_id,
      sync_token: pairing.sync_token,
      cursor: "0",
      device_name: pairing.device_name ?? state.device_name,
      platform: pairing.platform ?? state.platform,
      status: "syncing",
      last_error: null,
      snapshot_complete: false,
      history_manifest: null,
      canonical_revisions: {},
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
    (state) => ({
      ...state,
      sync_token: null,
      cursor: null,
      status: "unpaired",
      last_error: null,
      snapshot_complete: false,
      history_manifest: null,
    }),
    storage,
  );
}

export function beginLegacyHistorySync(
  input: {
    fingerprint: string;
    meeting_count: number;
    segment_count: number;
    summary_count: number;
    artifact_count: number;
  },
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  const counts = {
    meeting_count: Math.max(0, Math.floor(input.meeting_count || 0)),
    segment_count: Math.max(0, Math.floor(input.segment_count || 0)),
    summary_count: Math.max(0, Math.floor(input.summary_count || 0)),
    artifact_count: Math.max(0, Math.floor(input.artifact_count || 0)),
  };
  return updateSyncState(
    (state) => {
      const current = state.legacy_history;
      if (current?.fingerprint === input.fingerprint) return state;
      const phase: LegacyHistorySyncPhase =
        counts.meeting_count > 0
          ? "meetings"
          : counts.segment_count > 0
            ? "segments"
            : counts.summary_count > 0
              ? "summaries"
              : counts.artifact_count > 0
                ? "artifacts"
                : "complete";
      return {
        ...state,
        legacy_scan_complete: true,
        status:
          state.sync_token && phase !== "complete" ? "syncing" : state.status,
        legacy_history: {
          schema: 1,
          fingerprint: input.fingerprint,
          phase,
          offset: 0,
          page_end_offset: null,
          page_done: false,
          ...counts,
        },
      };
    },
    storage,
  );
}

export function completeLegacyHistoryScan(
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({ ...state, legacy_scan_complete: true }),
    storage,
  );
}

export function failLegacyHistoryScan(
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      legacy_scan_complete: false,
      status: state.sync_token ? "failed" : state.status,
      last_error: state.sync_token ? "本地历史完整性扫描失败" : state.last_error,
    }),
    storage,
  );
}

export function setLegacyHistoryPage(
  page: {
    fingerprint: string;
    phase: Exclude<LegacyHistorySyncPhase, "complete">;
    offset: number;
    next_offset: number;
    done: boolean;
  },
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => {
      const current = state.legacy_history;
      if (
        !current ||
        current.fingerprint !== page.fingerprint ||
        current.phase !== page.phase ||
        current.offset !== page.offset
      ) {
        return state;
      }
      return {
        ...state,
        legacy_history: {
          ...current,
          page_end_offset: Math.max(page.offset, page.next_offset),
          page_done: page.done,
        },
      };
    },
    storage,
  );
}

function nextLegacyHistoryPhase(
  state: LegacyHistorySyncState,
): LegacyHistorySyncPhase {
  if (state.phase === "meetings") return state.segment_count > 0 ? "segments" : state.summary_count > 0 ? "summaries" : state.artifact_count > 0 ? "artifacts" : "complete";
  if (state.phase === "segments") return state.summary_count > 0 ? "summaries" : state.artifact_count > 0 ? "artifacts" : "complete";
  if (state.phase === "summaries") return state.artifact_count > 0 ? "artifacts" : "complete";
  return "complete";
}

export function advanceLegacyHistoryPage(
  fingerprint: string,
  phase: Exclude<LegacyHistorySyncPhase, "complete">,
  nextOffset: number,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return updateSyncState(
    (state) => {
      const current = state.legacy_history;
      if (!current || current.fingerprint !== fingerprint || current.phase !== phase) {
        return state;
      }
      const pageEnd = current.page_end_offset ?? nextOffset;
      if (current.offset < pageEnd && nextOffset <= current.offset) return state;
      if (current.offset < pageEnd && nextOffset < pageEnd) {
        return {
          ...state,
          legacy_history: {
            ...current,
            offset: Math.max(current.offset, nextOffset),
            page_end_offset: null,
            page_done: false,
          },
        };
      }
      const nextPhase = current.page_done ? nextLegacyHistoryPhase(current) : phase;
      return {
        ...state,
        legacy_history: {
          ...current,
          phase: nextPhase,
          offset: nextPhase === phase ? Math.max(current.offset, nextOffset) : 0,
          page_end_offset: null,
          page_done: false,
        },
        legacy_scan_complete:
          state.legacy_scan_complete || nextPhase === "complete",
      };
    },
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
    result = {
      ...item,
      status: "pending",
      retry_count: 0,
      last_error: null,
      retryable: true,
    };
    return {
      ...state,
      status: state.sync_token ? "syncing" : state.status,
      outbox: [...state.outbox, result],
    };
  }, storage);
  return result;
}

export function pendingSyncOperations(
  limit = 20,
  storage: SyncStorage | null = browserStorage(),
): SyncOutboxItem[] {
  return loadSyncState(storage).outbox
    .filter((item) => item.status === "pending" || (item.status === "failed" && item.retryable))
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
    (state) => {
      const remaining = state.outbox.filter((item) => item.operation_id !== operationId);
      const failedItem = remaining.find((item) => item.status === "failed");
      const next = {
        ...state,
        outbox: remaining,
        last_error: failedItem?.last_error ?? null,
        last_synced_at: new Date().toISOString(),
      };
      return { ...next, status: settledSyncStatus(next) };
    },
    storage,
  );
}

export function failSyncOperation(
  operationId: string,
  error: string,
  storage: SyncStorage | null = browserStorage(),
  options: { retryable?: boolean } = {},
): SyncState {
  return updateSyncState(
    (state) => ({
      ...state,
      status: "failed",
      last_error: error,
      outbox: state.outbox.map((item) =>
        item.operation_id === operationId
          ? {
              ...item,
              status: "failed",
              retry_count: item.retry_count + 1,
              last_error: error,
              retryable: options.retryable ?? true,
            }
          : item,
      ),
    }),
    storage,
  );
}

export function syncEntityRevisionKey(entityType: SyncEntityType, entityId: string): string {
  return `${entityType}:${entityId}`;
}

export function knownSyncEntityRevision(
  entityType: SyncEntityType,
  entityId: string,
  storage: SyncStorage | null = browserStorage(),
): number | null {
  const revision = loadSyncState(storage).canonical_revisions[syncEntityRevisionKey(entityType, entityId)];
  return Number.isSafeInteger(revision) && revision >= 0 ? revision : null;
}

export function rememberSyncEntityRevision(
  entityType: SyncEntityType,
  entityId: string,
  revision: number,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  return rememberSyncEntityRevisions(
    [{ entity_type: entityType, entity_id: entityId, revision }],
    storage,
  );
}

export function rememberSyncEntityRevisions(
  changes: ReadonlyArray<{
    entity_type: SyncEntityType;
    entity_id: string;
    revision: number;
  }>,
  storage: SyncStorage | null = browserStorage(),
): SyncState {
  const valid = changes.filter(
    (change) =>
      typeof change.entity_id === "string" &&
      change.entity_id.length > 0 &&
      Number.isSafeInteger(change.revision) &&
      change.revision >= 0,
  );
  if (valid.length === 0) return loadSyncState(storage);
  return updateSyncState(
    (state) => {
      let changed = false;
      const revisions = { ...state.canonical_revisions };
      for (const change of valid) {
        const key = syncEntityRevisionKey(change.entity_type, change.entity_id);
        const current = revisions[key];
        if (current !== undefined && current >= change.revision) continue;
        revisions[key] = change.revision;
        changed = true;
      }
      if (!changed) return state;
      return {
        ...state,
        canonical_revisions: revisions,
      };
    },
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

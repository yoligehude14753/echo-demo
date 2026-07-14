import {
  loadSyncState,
  normalizeSyncCursor,
  setPairingState,
  type SyncEntityType,
  type SyncOutboxItem,
  type SyncState,
  type SyncStorage,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncState.ts";

export { normalizeSyncCursor };

export interface PairingClaimResponse {
  device_id: string;
  sync_token: string;
  cursor: string | null;
}

export interface SyncPushResponse {
  result: "applied" | "duplicate" | "conflict";
  current?: SyncChange | null;
}

export interface SyncChange {
  operation_id?: string | null;
  device_id?: string | null;
  cursor?: string | null;
  entity_type: SyncEntityType;
  entity_id: string;
  revision: number;
  updated_at: string;
  payload: Record<string, unknown>;
}

export function isSyncEntityType(value: unknown): value is SyncEntityType {
  return value === "transcript_segment" || value === "meeting_summary" || value === "memory";
}

export interface SyncChangesResponse {
  changes: SyncChange[];
  cursor: string | null;
  reset_required?: boolean;
  snapshot_required?: boolean;
}

export interface SyncSnapshotResponse {
  cursor: string | null;
  changes: SyncChange[];
}

export type SyncAuthMode = "session" | "sync";

export interface SyncTransport {
  request(path: string, init: RequestInit, auth: SyncAuthMode): Promise<Response>;
}

export class SyncApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null = null, code: string | null = null) {
    super(message);
    this.name = "SyncApiError";
    this.status = status;
    this.code = code;
  }
}

function readableDetail(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

async function readJson<T>(response: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    throw new SyncApiError("同步服务响应格式无效", response.status);
  }
  if (!response.ok) {
    const detailValue =
      typeof body === "object" && body !== null && "detail" in body
        ? (body as { detail?: unknown }).detail
        : undefined;
    const detail = readableDetail(detailValue);
    throw new SyncApiError(
      detail ? `同步服务请求失败：${detail.slice(0, 160)}` : "同步服务请求失败，请稍后重试",
      response.status,
      typeof body === "object" && body !== null && "code" in body
        ? String((body as { code?: unknown }).code ?? "")
        : null,
    );
  }
  return body as T;
}

function requireSyncToken(state: SyncState): string {
  if (!state.sync_token) throw new SyncApiError("设备尚未配对", 409, "not_paired");
  return state.sync_token;
}

function normalizeClaimCursor(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value === "number") {
    if (Number.isFinite(value) && Number.isInteger(value) && value >= 0) {
      return String(value);
    }
    throw new SyncApiError("配对响应包含无效的同步游标");
  }
  if (typeof value === "string") {
    const text = value.trim();
    const numeric = text === "" ? Number.NaN : Number(text);
    if (Number.isFinite(numeric) && Number.isInteger(numeric) && numeric >= 0) {
      return String(numeric);
    }
  }
  throw new SyncApiError("配对响应包含无效的同步游标");
}

function normalizeChangesCursor(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number" && Number.isFinite(value) && Number.isInteger(value) && value >= 0) {
    return String(value);
  }
  throw new SyncApiError("同步变更响应包含无效的同步游标");
}

export function normalizeClaimResponse(value: unknown): PairingClaimResponse {
  const deviceId =
    typeof value === "object" && value !== null
      ? (value as { device_id?: unknown }).device_id
      : undefined;
  const syncToken =
    typeof value === "object" && value !== null
      ? (value as { sync_token?: unknown }).sync_token
      : undefined;
  if (
    typeof deviceId !== "string" ||
    typeof syncToken !== "string"
  ) {
    throw new SyncApiError("配对响应缺少有效的设备身份或同步凭证");
  }
  const response = value as Partial<PairingClaimResponse>;
  return {
    device_id: deviceId,
    sync_token: syncToken,
    cursor: normalizeClaimCursor(response.cursor),
  };
}

function normalizeChanges(value: SyncChangesResponse | SyncChange[]): SyncChangesResponse {
  const changes = Array.isArray(value) ? value : value?.changes;
  if (!Array.isArray(changes)) throw new SyncApiError("同步变更响应格式无效");
  return {
    changes: changes.filter((change): change is SyncChange =>
      Boolean(
          change &&
          typeof change.entity_id === "string" &&
          isSyncEntityType(change.entity_type) &&
          typeof change.payload === "object" &&
          change.payload !== null,
      ),
    ),
    cursor: Array.isArray(value) ? null : normalizeChangesCursor(value.cursor),
    reset_required: Array.isArray(value) ? false : value.reset_required === true,
    snapshot_required: Array.isArray(value) ? false : value.snapshot_required === true,
  };
}

export class SyncHubClient {
  private readonly transport: SyncTransport;
  private readonly storage?: SyncStorage;

  constructor(transport: SyncTransport, storage?: SyncStorage) {
    this.transport = transport;
    this.storage = storage;
  }

  private state(): SyncState {
    return loadSyncState(this.storage);
  }

  async claimPairing(pairingCode: string): Promise<PairingClaimResponse> {
    const state = this.state();
    const code = pairingCode.trim();
    if (!code) throw new SyncApiError("请输入配对码", 400, "invalid_pairing_code");
    const response = await this.transport.request(
      "/hub/v1/pairings/claim",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pairing_code: code,
          device_id: state.device_id,
          device_name: state.device_name,
          platform: state.platform,
        }),
      },
      "session",
    );
    const result = normalizeClaimResponse(await readJson<unknown>(response));
    setPairingState({ ...result, device_id: result.device_id }, this.storage);
    return result;
  }

  async push(item: SyncOutboxItem): Promise<SyncPushResponse> {
    requireSyncToken(this.state());
    const response = await this.transport.request(
      "/hub/v1/sync/push",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operation_id: item.operation_id,
          device_id: item.device_id,
          entity_type: item.entity_type,
          entity_id: item.entity_id,
          base_revision: item.base_revision,
          updated_at: item.updated_at,
          payload: item.payload,
        }),
      },
      "sync",
    );
    return readJson<SyncPushResponse>(response);
  }

  async changes(cursor: string | null, limit = 100): Promise<SyncChangesResponse> {
    requireSyncToken(this.state());
    const params = new URLSearchParams({ cursor: cursor ?? "", limit: String(limit) });
    const response = await this.transport.request(
      `/hub/v1/sync/changes?${params.toString()}`,
      { method: "GET" },
      "sync",
    );
    return normalizeChanges(await readJson<SyncChangesResponse>(response));
  }

  async snapshot(): Promise<SyncSnapshotResponse> {
    requireSyncToken(this.state());
    const response = await this.transport.request(
      "/hub/v1/sync/snapshot",
      { method: "GET" },
      "sync",
    );
    const result = await readJson<SyncSnapshotResponse | SyncChangesResponse>(response);
    const changes = normalizeChanges(result);
    return { cursor: changes.cursor, changes: changes.changes };
  }
}

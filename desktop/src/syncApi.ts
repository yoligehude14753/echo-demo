import { configuredSyncHubBase } from "@/runtime";
import { apiTransport, ensureServerSession } from "@/session";
import { loadSyncState, type SyncStorage } from "@/syncState";
import { prepareSyncRequest } from "@/syncTransportHeaders";
import {
  SyncApiError,
  SyncHubClient as ProtocolSyncHubClient,
  type SyncAuthMode,
  type SyncTransport,
} from "@/syncProtocol";

const SYNC_TIMEOUT_MS = 20_000;
const SYNC_MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

async function requestWithDefaultTransport(
  path: string,
  init: RequestInit,
  auth: SyncAuthMode,
): Promise<Response> {
  const hubBase = configuredSyncHubBase();
  if (!hubBase) {
    throw new SyncApiError("同步网关未配置", 503, "sync_gateway_unconfigured");
  }
  const token =
    auth === "session"
      ? await ensureServerSession()
      : loadSyncState().sync_token;
  const url = `${hubBase}${path}`;
  const prepared = prepareSyncRequest(init, auth, token);
  return apiTransport(url, prepared.init, {
    timeoutMs: SYNC_TIMEOUT_MS,
    maxResponseBytes: SYNC_MAX_RESPONSE_BYTES,
    throwHttpErrors: false,
    targetOrigin: new URL(hubBase).origin,
    bearerToken: prepared.bearerToken,
  });
}

export const defaultSyncTransport: SyncTransport = {
  request: requestWithDefaultTransport,
};

export class SyncHubClient extends ProtocolSyncHubClient {
  constructor(transport: SyncTransport = defaultSyncTransport, storage?: SyncStorage) {
    super(transport, storage);
  }
}

export function createSyncHubClient(
  transport: SyncTransport,
  storage?: SyncStorage,
): SyncHubClient {
  return new SyncHubClient(transport, storage);
}

export {
  SyncApiError,
  type PairingClaimResponse,
  type SyncChange,
  type SyncChangesResponse,
  type SyncPushResponse,
  type SyncSnapshotResponse,
} from "@/syncProtocol";
export type { SyncAuthMode, SyncTransport } from "@/syncProtocol";

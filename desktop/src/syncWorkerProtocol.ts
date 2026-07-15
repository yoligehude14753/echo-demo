import {
  isSyncEntityType,
  normalizeSyncCursor,
  type SyncChange,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncProtocol.ts";

function readFrameCursor(value: unknown): { valid: true; cursor: string | null } | { valid: false } {
  try {
    return { valid: true, cursor: normalizeSyncCursor(value) };
  } catch {
    return { valid: false };
  }
}

export interface ParsedSyncFrame {
  change?: SyncChange;
  cursor?: string | null;
  snapshotRequired: boolean;
  ping: boolean;
}

export function syncHubWebSocketUrl(base: string, cursor: string | null): string {
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/hub/v1/sync/events";
  url.search = "";
  url.searchParams.set("cursor", cursor ?? "");
  return url.toString();
}

function validSyncChange(value: unknown): value is SyncChange {
  if (!value || typeof value !== "object") return false;
  const change = value as Partial<SyncChange>;
  return (
    isSyncEntityType(change.entity_type) &&
    typeof change.entity_id === "string" &&
    typeof change.payload === "object" &&
    change.payload !== null
  );
}

export function parseSyncFrame(raw: unknown): ParsedSyncFrame | null {
  if (typeof raw !== "string" || raw.length > 1024 * 1024) return null;
  let value: Record<string, unknown>;
  try {
    value = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
  const type = typeof value.type === "string" ? value.type : "";
  if (type === "server_ping" || type === "ping") {
    return { ping: true, snapshotRequired: false };
  }
  if (type === "server_resync" || type === "snapshot_required" || type === "cursor_invalid") {
    const cursor = readFrameCursor(value.cursor);
    if (!cursor.valid) return null;
    return {
      cursor: cursor.cursor,
      snapshotRequired: true,
      ping: false,
    };
  }
  if (type === "server_hello" || type === "server_sync") {
    const cursor = readFrameCursor(value.cursor);
    if (!cursor.valid) return null;
    return {
      cursor: cursor.cursor,
      snapshotRequired: false,
      ping: false,
    };
  }
  const candidate =
    type === "change" || type === "sync.change" ? value.change ?? value.payload : value;
  if (!validSyncChange(candidate)) return null;
  const change = candidate as SyncChange;
  const rawCursor = change.cursor ?? value.cursor;
  const cursor = readFrameCursor(rawCursor);
  if (!cursor.valid) return null;
  return {
    change: {
      ...change,
      cursor: cursor.cursor,
    },
    cursor: cursor.cursor,
    snapshotRequired: false,
    ping: false,
  };
}

import {
  isSyncEntityType,
  type SyncChange,
  // @ts-expect-error Node's strip-types runner executes the source test directly.
} from "./syncProtocol.ts";

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
    return {
      cursor: typeof value.cursor === "string" ? value.cursor : null,
      snapshotRequired: true,
      ping: false,
    };
  }
  if (type === "server_hello" || type === "server_sync") {
    return {
      cursor: typeof value.cursor === "string" ? value.cursor : null,
      snapshotRequired: false,
      ping: false,
    };
  }
  const candidate =
    type === "change" || type === "sync.change" ? value.change ?? value.payload : value;
  if (!validSyncChange(candidate)) return null;
  const change = candidate as SyncChange;
  return {
    change: {
      ...change,
      cursor: change.cursor ?? (typeof value.cursor === "string" ? value.cursor : null),
    },
    cursor: typeof value.cursor === "string" ? value.cursor : change.cursor,
    snapshotRequired: false,
    ping: false,
  };
}

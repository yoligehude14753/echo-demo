import {
  advanceLegacyHistoryPage,
  enqueueSyncOperation,
  ensureSyncDeviceId,
  knownSyncEntityRevision,
  loadSyncState,
  setLegacyHistoryPage,
  type LegacyHistorySyncPhase,
  type SyncStorage,
} from "@/syncState";
import { isSyncEntityType, type SyncChange } from "@/syncProtocol";

type LegacyHistoryPagePhase = Exclude<LegacyHistorySyncPhase, "complete">;

interface LegacyHistoryPageItem {
  operation_id: string;
  entity_type: SyncChange["entity_type"];
  entity_id: string;
  updated_at: string;
  payload: Record<string, unknown>;
}

interface LegacyHistoryPage {
  phase: LegacyHistoryPagePhase;
  offset: number;
  items: LegacyHistoryPageItem[];
  nextOffset: number;
  done: boolean;
}

function isPagePhase(value: unknown): value is LegacyHistoryPagePhase {
  return (
    value === "meetings" ||
    value === "segments" ||
    value === "summaries" ||
    value === "artifacts"
  );
}

function normalizePage(value: unknown): LegacyHistoryPage | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<LegacyHistoryPage>;
  const offset = raw.offset;
  const nextOffset = raw.nextOffset;
  if (
    !isPagePhase(raw.phase) ||
    typeof offset !== "number" ||
    !Number.isSafeInteger(offset) ||
    offset < 0 ||
    typeof nextOffset !== "number" ||
    !Number.isSafeInteger(nextOffset) ||
    nextOffset < offset ||
    !Array.isArray(raw.items)
  ) {
    return null;
  }
  const items = raw.items.filter((item): item is LegacyHistoryPageItem => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as Partial<LegacyHistoryPageItem>;
    return (
      typeof candidate.operation_id === "string" &&
      candidate.operation_id.length > 0 &&
      typeof candidate.entity_id === "string" &&
      isSyncEntityType(candidate.entity_type) &&
      typeof candidate.updated_at === "string" &&
      !!candidate.payload &&
      typeof candidate.payload === "object" &&
      !Array.isArray(candidate.payload)
    );
  });
  return {
    phase: raw.phase,
    offset,
    items,
    nextOffset,
    done: raw.done === true,
  };
}

function currentPageItems(state: ReturnType<typeof loadSyncState>): number {
  const legacy = state.legacy_history;
  if (!legacy || legacy.phase === "complete" || legacy.page_end_offset === null) return 0;
  return state.outbox.filter(
    (item) =>
      item.legacy_page?.fingerprint === legacy.fingerprint &&
      item.legacy_page.phase === legacy.phase &&
      item.legacy_page.offset === legacy.offset,
  ).length;
}

export function legacyHistoryPagePending(storage?: SyncStorage): boolean {
  const state = loadSyncState(storage);
  return currentPageItems(state) > 0;
}

export function advanceCompletedLegacyHistoryPage(storage?: SyncStorage): boolean {
  const state = loadSyncState(storage);
  const legacy = state.legacy_history;
  if (!legacy || legacy.phase === "complete" || legacy.page_end_offset === null) return false;
  if (currentPageItems(state) > 0) return false;
  advanceLegacyHistoryPage(
    legacy.fingerprint,
    legacy.phase,
    legacy.page_end_offset,
    storage,
  );
  return true;
}

export async function refillLegacyHistoryPage(
  storage?: SyncStorage,
): Promise<number> {
  if (typeof window === "undefined") return 0;
  const state = loadSyncState(storage);
  const legacy = state.legacy_history;
  if (!state.sync_token || !legacy || legacy.phase === "complete") return 0;
  if (legacy.page_end_offset !== null) return 0;
  if (currentPageItems(state) > 0) return 0;
  const loader = window.echo?.loadLocalLegacyHistoryPage;
  if (!loader) return 0;
  const limit = legacy.phase === "artifacts" ? 2 : 20;
  const page = normalizePage(
    await loader(legacy.phase, legacy.offset, limit),
  );
  if (
    !page ||
    page.phase !== legacy.phase ||
    page.offset !== legacy.offset
  ) {
    throw new Error("本地历史同步分页响应无效");
  }
  setLegacyHistoryPage(
    {
      fingerprint: legacy.fingerprint,
      phase: page.phase,
      offset: page.offset,
      next_offset: page.nextOffset,
      done: page.done,
    },
    storage,
  );
  const deviceId = ensureSyncDeviceId(storage);
  for (const item of page.items) {
    const baseRevision = knownSyncEntityRevision(
      item.entity_type,
      item.entity_id,
      storage,
    ) ?? 0;
    enqueueSyncOperation(
      {
        operation_id: item.operation_id,
        device_id: deviceId,
        entity_type: item.entity_type,
        entity_id: item.entity_id,
        base_revision: baseRevision,
        updated_at: item.updated_at,
        payload: item.payload,
        legacy_page: {
          fingerprint: legacy.fingerprint,
          phase: page.phase,
          offset: page.offset,
        },
      },
      storage,
    );
  }
  return page.items.length;
}

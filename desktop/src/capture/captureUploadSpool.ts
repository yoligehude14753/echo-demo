// Durable backlog capacity is independent from the small renderer enqueue
// staging budget. The active partition owns one complete partition budget;
// recovery writers may not consume that final global slice.
export const CAPTURE_SPOOL_MAX_ITEMS = 8_192;
export const CAPTURE_SPOOL_GLOBAL_MAX_ITEMS = 32_768;
export const CAPTURE_SPOOL_MAX_BYTES = 1_024 * 1024 * 1024;
export const CAPTURE_SPOOL_GLOBAL_MAX_BYTES = 4_096 * 1024 * 1024;
export const CAPTURE_SPOOL_ACTIVE_RESERVED_ITEMS = CAPTURE_SPOOL_MAX_ITEMS;
export const CAPTURE_SPOOL_ACTIVE_RESERVED_BYTES = CAPTURE_SPOOL_MAX_BYTES;
/**
 * Durable cutover marker for the post-claim-storm queue. Bump this value only
 * when an intentional, user-visible queue generation cutover is shipped.
 * The first open of a database without this marker atomically drops all
 * pre-cutover chunks; subsequent restarts retain only this generation.
 */
export const CAPTURE_SPOOL_CUTOVER_GENERATION = "echodesk-capture-2026-08-03-g5";
// The backend durable-claim path is more expensive than the multipart body
// ingress. Keep one process-wide budget and reserve most of it for the live
// capture partition; recovery must never turn a new session into a claim storm.
export const CAPTURE_UPLOAD_MAX_PARALLEL_REQUESTS = 4;
export const CAPTURE_UPLOAD_ACTIVE_PARTITION_CONCURRENCY = 3;
export const CAPTURE_UPLOAD_RECOVERY_PARTITION_CONCURRENCY = 1;
// A provider-wide outage must not make every durable item wake on the normal
// one-second transport retry ladder. Keep the item in the spool and let a
// later probe retry it, but give the backend/ASR control plane time to recover.
export const CAPTURE_SYSTEMIC_FAILURE_BACKOFF_MS = 60_000;
// Offline, sleep and app-restart recovery must survive ordinary outages while
// remaining bounded. Capacity limits remain the hard admission boundary.
export const CAPTURE_SPOOL_TTL_MS = 24 * 60 * 60 * 1_000;
export const CAPTURE_RETRY_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;

export interface CaptureSpoolScope {
  deviceId: string;
  /** 旧 IndexedDB 记录可能没有该字段；仅恢复读取兼容，新写入仍强制要求。 */
  captureSessionId?: string;
  source: "desktop";
}

export type CaptureSpoolLane = "free" | "formal";

export interface CaptureSpoolItem {
  ordinal?: number;
  partition: string;
  origin: string;
  principalScope: string;
  wav: Blob;
  byteSize: number;
  capturedAtMs: number;
  expiresAtMs: number;
  meetingId: string | null;
  scope: CaptureSpoolScope;
  segmentId: string;
  idempotencyKey: string;
  retryCount: number;
  nextAttemptAtMs: number;
}

export interface CaptureSpoolSnapshot {
  depth: number;
  bytes: number;
  globalDepth: number;
  globalBytes: number;
  partitionCount: number;
  expired: number;
}

export interface CaptureSpoolPartitionSummary {
  partition: string;
  origin: string;
  principalScope: string;
  deviceId: string;
  /** 仅用于兼容校验，不得投影到日志或 diagnostics。 */
  captureSessionId?: string;
  depth: number;
  bytes: number;
  oldestOrdinal: number;
  oldestCapturedAtMs: number;
  nextAttemptAtMs: number;
}

export interface CaptureSpoolInventory {
  partitions: CaptureSpoolPartitionSummary[];
  globalDepth: number;
  globalBytes: number;
  expired: number;
}

interface CaptureSpoolGlobalMetadata {
  key: "global";
  kind: "global";
  depth: number;
  bytes: number;
}

interface CaptureSpoolPartitionMetadata extends CaptureSpoolPartitionSummary {
  key: string;
  kind: "partition";
}

interface CaptureSpoolGenerationMetadata {
  key: "generation";
  kind: "generation";
  generation: string;
  cutoverAtMs: number;
}

type CaptureSpoolMetadata =
  | CaptureSpoolGlobalMetadata
  | CaptureSpoolPartitionMetadata
  | CaptureSpoolGenerationMetadata;

const CAPTURE_SPOOL_CHUNKS_STORE = "chunks";
const CAPTURE_SPOOL_METADATA_STORE = "metadata";
const CAPTURE_SPOOL_GLOBAL_METADATA_KEY = "global";
const CAPTURE_SPOOL_GENERATION_METADATA_KEY = "generation";
const CAPTURE_SPOOL_EXPIRES_INDEX = "expiresAtMs";
const CAPTURE_SPOOL_PARTITION_INDEX = "partition";

function partitionMetadataKey(partition: string): string {
  return `partition:${partition}`;
}

export interface CaptureSpoolEnqueueOptions {
  /** 当前实时分区；其它分区写入不得侵占为它保留的全局容量。 */
  activePartition?: string;
}

export type CaptureSpoolRejectReason =
  | "count_limit"
  | "byte_limit"
  | "global_count_limit"
  | "global_byte_limit"
  | "active_count_reserve"
  | "active_byte_reserve";

export type CaptureSpoolHardCapacityRejectReason = Extract<
  CaptureSpoolRejectReason,
  "count_limit" | "byte_limit" | "global_count_limit" | "global_byte_limit"
>;

export function isCaptureSpoolHardCapacityRejection(
  reason: CaptureSpoolRejectReason,
): reason is CaptureSpoolHardCapacityRejectReason {
  return (
    reason === "count_limit" ||
    reason === "byte_limit" ||
    reason === "global_count_limit" ||
    reason === "global_byte_limit"
  );
}

export type CaptureSpoolEnqueueResult =
  | { accepted: true; snapshot: CaptureSpoolSnapshot }
  | {
      accepted: false;
      reason: CaptureSpoolRejectReason;
      snapshot: CaptureSpoolSnapshot;
    };

export interface CaptureUploadSpool {
  enqueue(
    item: CaptureSpoolItem,
    nowMs?: number,
    options?: CaptureSpoolEnqueueOptions,
  ): Promise<CaptureSpoolEnqueueResult>;
  peek(partition: string, nowMs?: number): Promise<{
    item: CaptureSpoolItem | null;
    snapshot: CaptureSpoolSnapshot;
  }>;
  peekBatch?(partition: string, limit: number, nowMs?: number): Promise<{
    items: CaptureSpoolItem[];
    snapshot: CaptureSpoolSnapshot;
  }>;
  acknowledge(ordinal: number): Promise<void>;
  markRetry(ordinal: number, retryCount: number, nextAttemptAtMs: number): Promise<void>;
  snapshot(partition: string, nowMs?: number): Promise<CaptureSpoolSnapshot>;
  listPartitions(nowMs?: number): Promise<CaptureSpoolInventory>;
}

function validItem(item: CaptureSpoolItem): void {
  const captureSessionId = item.scope.captureSessionId;
  const isKnownPartition = Boolean(
    captureSessionId &&
      (item.partition ===
        captureSpoolPartition(
          item.origin,
          item.principalScope,
          item.scope.deviceId,
          captureSessionId,
        ) ||
        item.partition ===
          captureSpoolPartition(
            item.origin,
            item.principalScope,
            item.scope.deviceId,
            captureSessionId,
            "formal",
          )),
  );
  if (
    !item.partition ||
    !item.origin ||
    !item.principalScope ||
    !(item.wav instanceof Blob) ||
    !Number.isSafeInteger(item.byteSize) ||
    item.byteSize < 1 ||
    item.byteSize !== item.wav.size ||
    !Number.isSafeInteger(item.capturedAtMs) ||
    !Number.isSafeInteger(item.expiresAtMs) ||
    item.expiresAtMs <= item.capturedAtMs ||
    !item.segmentId ||
    !item.idempotencyKey ||
    !item.scope.deviceId ||
    !captureSessionId ||
    item.scope.source !== "desktop" ||
    !isKnownPartition
  ) {
    throw new Error("capture spool item is invalid");
  }
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("capture spool request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error("capture spool transaction aborted"));
    transaction.onerror = () => reject(transaction.error ?? new Error("capture spool transaction failed"));
  });
}

function emptyGlobalMetadata(): CaptureSpoolGlobalMetadata {
  return { key: "global", kind: "global", depth: 0, bytes: 0 };
}

function partitionMetadata(
  item: CaptureSpoolItem,
  ordinal: number,
): CaptureSpoolPartitionMetadata {
  return {
    key: partitionMetadataKey(item.partition),
    kind: "partition",
    partition: item.partition,
    origin: item.origin,
    principalScope: item.principalScope,
    deviceId: item.scope.deviceId,
    captureSessionId: item.scope.captureSessionId,
    depth: 1,
    bytes: item.byteSize,
    oldestOrdinal: ordinal,
    oldestCapturedAtMs: item.capturedAtMs,
    nextAttemptAtMs: item.nextAttemptAtMs,
  };
}

function configureCaptureSpoolSchema(
  database: IDBDatabase,
  transaction: IDBTransaction,
): void {
  const chunks = database.objectStoreNames.contains(CAPTURE_SPOOL_CHUNKS_STORE)
    ? transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE)
    : database.createObjectStore(CAPTURE_SPOOL_CHUNKS_STORE, {
        keyPath: "ordinal",
        autoIncrement: true,
      });
  if (!chunks.indexNames.contains(CAPTURE_SPOOL_PARTITION_INDEX)) {
    chunks.createIndex(CAPTURE_SPOOL_PARTITION_INDEX, "partition", {
      unique: false,
    });
  }
  if (!chunks.indexNames.contains(CAPTURE_SPOOL_EXPIRES_INDEX)) {
    chunks.createIndex(CAPTURE_SPOOL_EXPIRES_INDEX, "expiresAtMs", {
      unique: false,
    });
  }
  if (!database.objectStoreNames.contains(CAPTURE_SPOOL_METADATA_STORE)) {
    database.createObjectStore(CAPTURE_SPOOL_METADATA_STORE, {
      keyPath: "key",
    });
  }
}

function captureSpoolSchemaReady(database: IDBDatabase): boolean {
  if (
    !database.objectStoreNames.contains(CAPTURE_SPOOL_CHUNKS_STORE) ||
    !database.objectStoreNames.contains(CAPTURE_SPOOL_METADATA_STORE)
  ) {
    return false;
  }
  const transaction = database.transaction(CAPTURE_SPOOL_CHUNKS_STORE, "readonly");
  const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
  return (
    chunks.indexNames.contains(CAPTURE_SPOOL_PARTITION_INDEX) &&
    chunks.indexNames.contains(CAPTURE_SPOOL_EXPIRES_INDEX)
  );
}

function openDatabaseRequest(
  databaseName: string,
  version?: number,
): Promise<IDBDatabase> {
  return new Promise<IDBDatabase>((resolve, reject) => {
    let settled = false;
    const request = version === undefined
      ? indexedDB.open(databaseName)
      : indexedDB.open(databaseName, version);
    request.onupgradeneeded = () => {
      const transaction = request.transaction;
      if (!transaction) throw new Error("capture spool upgrade transaction missing");
      configureCaptureSpoolSchema(request.result, transaction);
    };
    request.onsuccess = () => {
      if (settled) {
        request.result.close();
        return;
      }
      settled = true;
      resolve(request.result);
    };
    request.onerror = () => {
      if (settled) return;
      settled = true;
      reject(request.error ?? new Error("capture spool open failed"));
    };
    request.onblocked = () => {
      if (settled) return;
      settled = true;
      reject(new Error("capture spool upgrade is blocked"));
    };
  });
}

async function openCaptureSpoolDatabase(
  databaseName: string,
): Promise<IDBDatabase> {
  let database = await openDatabaseRequest(databaseName);
  if (!captureSpoolSchemaReady(database)) {
    const nextVersion = database.version + 1;
    database.close();
    database = await openDatabaseRequest(databaseName, nextVersion);
  }
  return database;
}

async function ensureCaptureSpoolGeneration(database: IDBDatabase): Promise<void> {
  const transaction = database.transaction(
    [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
    "readwrite",
  );
  const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
  const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
  const current = await requestResult(
    metadata.get(CAPTURE_SPOOL_GENERATION_METADATA_KEY),
  ) as CaptureSpoolGenerationMetadata | undefined;
  if (current?.generation === CAPTURE_SPOOL_CUTOVER_GENERATION) {
    await transactionDone(transaction);
    return;
  }

  // This transaction is the durable cutover boundary. It is intentionally
  // independent from metadata reconciliation so a crash cannot leave an old
  // queue visible without a generation marker on the next startup.
  await requestResult(chunks.clear());
  await requestResult(metadata.clear());
  metadata.put({
    key: CAPTURE_SPOOL_GENERATION_METADATA_KEY,
    kind: "generation",
    generation: CAPTURE_SPOOL_CUTOVER_GENERATION,
    cutoverAtMs: Date.now(),
  } satisfies CaptureSpoolGenerationMetadata);
  await transactionDone(transaction);
}

async function reconcileCaptureSpoolMetadata(database: IDBDatabase): Promise<void> {
  const transaction = database.transaction(
    [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
    "readwrite",
  );
  const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
  const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
  const generation = await requestResult(
    metadata.get(CAPTURE_SPOOL_GENERATION_METADATA_KEY),
  ) as CaptureSpoolGenerationMetadata | undefined;
  await requestResult(metadata.clear());
  const global = emptyGlobalMetadata();
  const partitions = new Map<string, CaptureSpoolPartitionMetadata>();
  await new Promise<void>((resolve, reject) => {
    const cursorRequest = chunks.openCursor();
    cursorRequest.onerror = () => reject(
      cursorRequest.error ?? new Error("capture spool reconcile failed"),
    );
    cursorRequest.onsuccess = () => {
      const cursor = cursorRequest.result;
      if (!cursor) {
        resolve();
        return;
      }
      const item = cursor.value as CaptureSpoolItem;
      const ordinal = item.ordinal ?? Number(cursor.primaryKey);
      global.depth += 1;
      global.bytes += item.byteSize;
      const current = partitions.get(item.partition);
      if (current) {
        current.depth += 1;
        current.bytes += item.byteSize;
      } else {
        partitions.set(item.partition, partitionMetadata(item, ordinal));
      }
      cursor.continue();
    };
  });
  metadata.put(global);
  for (const partition of partitions.values()) metadata.put(partition);
  if (generation) metadata.put(generation);
  await transactionDone(transaction);
}

async function readMetadataSnapshot(
  metadata: IDBObjectStore,
  partition: string,
  expired: number,
): Promise<CaptureSpoolSnapshot> {
  const globalRequest = metadata.get(CAPTURE_SPOOL_GLOBAL_METADATA_KEY);
  const generationRequest = metadata.get(CAPTURE_SPOOL_GENERATION_METADATA_KEY);
  const partitionRequest = metadata.get(partitionMetadataKey(partition));
  const metadataCountRequest = metadata.count();
  const [globalValue, generationValue, partitionValue, metadataCount] = await Promise.all([
    requestResult(globalRequest),
    requestResult(generationRequest),
    requestResult(partitionRequest),
    requestResult(metadataCountRequest),
  ]);
  const global = (globalValue as CaptureSpoolGlobalMetadata | undefined) ??
    emptyGlobalMetadata();
  const scoped = partitionValue as CaptureSpoolPartitionMetadata | undefined;
  return {
    depth: scoped?.depth ?? 0,
    bytes: scoped?.bytes ?? 0,
    globalDepth: global.depth,
    globalBytes: global.bytes,
    partitionCount: Math.max(
      0,
      metadataCount - 1 - (generationValue ? 1 : 0),
    ),
    expired,
  };
}

function readOldestPartitionItem(
  chunks: IDBObjectStore,
  partition: string,
): Promise<CaptureSpoolItem | null> {
  return new Promise((resolve, reject) => {
    const request = chunks
      .index(CAPTURE_SPOOL_PARTITION_INDEX)
      .openCursor(IDBKeyRange.only(partition), "next");
    request.onerror = () => reject(
      request.error ?? new Error("capture spool oldest lookup failed"),
    );
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(null);
        return;
      }
      const value = cursor.value as CaptureSpoolItem;
      const ordinal = value.ordinal ?? Number(cursor.primaryKey);
      resolve(value.ordinal === ordinal ? value : { ...value, ordinal });
    };
  });
}

function readPartitionBatch(
  chunks: IDBObjectStore,
  partition: string,
  limit: number,
): Promise<CaptureSpoolItem[]> {
  return new Promise((resolve, reject) => {
    const items: CaptureSpoolItem[] = [];
    const request = chunks
      .index(CAPTURE_SPOOL_PARTITION_INDEX)
      .openCursor(IDBKeyRange.only(partition), "next");
    request.onerror = () => reject(
      request.error ?? new Error("capture spool batch lookup failed"),
    );
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor || items.length >= limit) {
        resolve(items);
        return;
      }
      const value = cursor.value as CaptureSpoolItem;
      const ordinal = value.ordinal ?? Number(cursor.primaryKey);
      items.push(value.ordinal === ordinal ? value : { ...value, ordinal });
      cursor.continue();
    };
  });
}

async function refreshPartitionOldest(
  chunks: IDBObjectStore,
  metadata: IDBObjectStore,
  current: CaptureSpoolPartitionMetadata,
): Promise<void> {
  if (current.depth <= 0) {
    await requestResult(metadata.delete(current.key));
    return;
  }
  const oldest = await readOldestPartitionItem(chunks, current.partition);
  if (!oldest || oldest.ordinal === undefined) {
    await requestResult(metadata.delete(current.key));
    return;
  }
  metadata.put({
    ...current,
    oldestOrdinal: oldest.ordinal,
    oldestCapturedAtMs: oldest.capturedAtMs,
    nextAttemptAtMs: oldest.nextAttemptAtMs,
  });
}

async function pruneExpiredCaptureItems(
  chunks: IDBObjectStore,
  metadata: IDBObjectStore,
  nowMs: number,
): Promise<number> {
  const expiredByPartition = new Map<string, { count: number; bytes: number }>();
  let expired = 0;
  let expiredBytes = 0;
  await new Promise<void>((resolve, reject) => {
    const request = chunks
      .index(CAPTURE_SPOOL_EXPIRES_INDEX)
      .openCursor(IDBKeyRange.upperBound(nowMs), "next");
    request.onerror = () => reject(
      request.error ?? new Error("capture spool expiry lookup failed"),
    );
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve();
        return;
      }
      const item = cursor.value as CaptureSpoolItem;
      expired += 1;
      expiredBytes += item.byteSize;
      const aggregate = expiredByPartition.get(item.partition) ?? {
        count: 0,
        bytes: 0,
      };
      aggregate.count += 1;
      aggregate.bytes += item.byteSize;
      expiredByPartition.set(item.partition, aggregate);
      cursor.delete();
      cursor.continue();
    };
  });
  if (expired === 0) return 0;

  const globalValue = await requestResult(
    metadata.get(CAPTURE_SPOOL_GLOBAL_METADATA_KEY),
  ) as CaptureSpoolGlobalMetadata | undefined;
  const global = globalValue ?? emptyGlobalMetadata();
  metadata.put({
    ...global,
    depth: Math.max(0, global.depth - expired),
    bytes: Math.max(0, global.bytes - expiredBytes),
  });
  for (const [partition, removed] of expiredByPartition) {
    const value = await requestResult(metadata.get(partitionMetadataKey(partition))) as
      CaptureSpoolPartitionMetadata | undefined;
    if (!value) continue;
    const current = {
      ...value,
      depth: Math.max(0, value.depth - removed.count),
      bytes: Math.max(0, value.bytes - removed.bytes),
    };
    await refreshPartitionOldest(chunks, metadata, current);
  }
  return expired;
}

function capacityRejection(
  snapshot: CaptureSpoolSnapshot,
  item: CaptureSpoolItem,
  activePartition: string,
): CaptureSpoolRejectReason | null {
  if (snapshot.depth >= CAPTURE_SPOOL_MAX_ITEMS) return "count_limit";
  if (snapshot.bytes + item.byteSize > CAPTURE_SPOOL_MAX_BYTES) return "byte_limit";
  if (item.partition !== activePartition) {
    if (
      snapshot.globalDepth >=
      CAPTURE_SPOOL_GLOBAL_MAX_ITEMS - CAPTURE_SPOOL_ACTIVE_RESERVED_ITEMS
    ) {
      return "active_count_reserve";
    }
    if (
      snapshot.globalBytes + item.byteSize >
      CAPTURE_SPOOL_GLOBAL_MAX_BYTES - CAPTURE_SPOOL_ACTIVE_RESERVED_BYTES
    ) {
      return "active_byte_reserve";
    }
  }
  if (snapshot.globalDepth >= CAPTURE_SPOOL_GLOBAL_MAX_ITEMS) {
    return "global_count_limit";
  }
  if (snapshot.globalBytes + item.byteSize > CAPTURE_SPOOL_GLOBAL_MAX_BYTES) {
    return "global_byte_limit";
  }
  return null;
}

export class IndexedDbCaptureUploadSpool implements CaptureUploadSpool {
  private readonly databaseName: string;
  private databasePromise: Promise<IDBDatabase> | null = null;

  constructor(databaseName = "echodesk.captureUploadSpool.v1") {
    this.databaseName = databaseName;
  }

  private database(): Promise<IDBDatabase> {
    if (this.databasePromise) return this.databasePromise;
    if (typeof indexedDB === "undefined") {
      throw new Error("durable capture spool is unavailable");
    }
    // Existing v1 queues are upgraded in place. A single startup reconcile
    // materializes authoritative counters; hot enqueue/peek/list paths then
    // use metadata plus bounded indexes instead of rescanning every chunk.
    const pending = (async () => {
      const database = await openCaptureSpoolDatabase(this.databaseName);
      await ensureCaptureSpoolGeneration(database);
      await reconcileCaptureSpoolMetadata(database);
      database.onversionchange = () => {
        database.close();
        if (this.databasePromise === pending) this.databasePromise = null;
      };
      return database;
    })();
    this.databasePromise = pending;
    void pending.catch(() => {
      if (this.databasePromise === pending) this.databasePromise = null;
    });
    return pending;
  }

  async enqueue(
    item: CaptureSpoolItem,
    nowMs = Date.now(),
    options: CaptureSpoolEnqueueOptions = {},
  ): Promise<CaptureSpoolEnqueueResult> {
    validItem(item);
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const store = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const expired = await pruneExpiredCaptureItems(store, metadata, nowMs);
    const snapshot = await readMetadataSnapshot(metadata, item.partition, expired);
    const rejection = capacityRejection(
      snapshot,
      item,
      options.activePartition ?? item.partition,
    );
    if (rejection) {
      await transactionDone(transaction);
      return {
        accepted: false,
        reason: rejection,
        snapshot,
      };
    }
    const currentPartition = await requestResult(
      metadata.get(partitionMetadataKey(item.partition)),
    ) as CaptureSpoolPartitionMetadata | undefined;
    const { ordinal: _ordinal, ...persisted } = item;
    void _ordinal;
    const ordinal = Number(await requestResult(store.add(persisted)));
    metadata.put({
      key: CAPTURE_SPOOL_GLOBAL_METADATA_KEY,
      kind: "global",
      depth: snapshot.globalDepth + 1,
      bytes: snapshot.globalBytes + item.byteSize,
    } satisfies CaptureSpoolGlobalMetadata);
    metadata.put(
      currentPartition
        ? {
            ...currentPartition,
            depth: currentPartition.depth + 1,
            bytes: currentPartition.bytes + item.byteSize,
          }
        : partitionMetadata({ ...item, ordinal }, ordinal),
    );
    await transactionDone(transaction);
    return {
      accepted: true,
      snapshot: {
        depth: snapshot.depth + 1,
        bytes: snapshot.bytes + item.byteSize,
        globalDepth: snapshot.globalDepth + 1,
        globalBytes: snapshot.globalBytes + item.byteSize,
        partitionCount: snapshot.partitionCount + (currentPartition ? 0 : 1),
        expired,
      },
    };
  }

  async peek(partition: string, nowMs = Date.now()) {
    const result = await this.peekBatch(partition, 1, nowMs);
    return { item: result.items[0] ?? null, snapshot: result.snapshot };
  }

  async peekBatch(partition: string, limit: number, nowMs = Date.now()) {
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const expired = await pruneExpiredCaptureItems(chunks, metadata, nowMs);
    const items = await readPartitionBatch(
      chunks,
      partition,
      normalizedBatchLimit(limit),
    );
    const snapshot = await readMetadataSnapshot(metadata, partition, expired);
    await transactionDone(transaction);
    return { items, snapshot };
  }

  async acknowledge(ordinal: number): Promise<void> {
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const item = await requestResult(chunks.get(ordinal)) as
      CaptureSpoolItem | undefined;
    if (!item) {
      await transactionDone(transaction);
      return;
    }
    const globalRequest = metadata.get(CAPTURE_SPOOL_GLOBAL_METADATA_KEY);
    const partitionRequest = metadata.get(partitionMetadataKey(item.partition));
    const [globalValue, partitionValue] = await Promise.all([
      requestResult(globalRequest),
      requestResult(partitionRequest),
    ]);
    await requestResult(chunks.delete(ordinal));
    const global = (globalValue as CaptureSpoolGlobalMetadata | undefined) ??
      emptyGlobalMetadata();
    metadata.put({
      ...global,
      depth: Math.max(0, global.depth - 1),
      bytes: Math.max(0, global.bytes - item.byteSize),
    });
    const partition = partitionValue as CaptureSpoolPartitionMetadata | undefined;
    if (partition) {
      const current = {
        ...partition,
        depth: Math.max(0, partition.depth - 1),
        bytes: Math.max(0, partition.bytes - item.byteSize),
      };
      if (partition.oldestOrdinal === ordinal) {
        await refreshPartitionOldest(chunks, metadata, current);
      } else {
        metadata.put(current);
      }
    }
    await transactionDone(transaction);
  }

  async markRetry(
    ordinal: number,
    retryCount: number,
    nextAttemptAtMs: number,
  ): Promise<void> {
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const store = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const current = await requestResult(store.get(ordinal)) as CaptureSpoolItem | undefined;
    if (current) {
      store.put({ ...current, retryCount, nextAttemptAtMs });
      const partition = await requestResult(
        metadata.get(partitionMetadataKey(current.partition)),
      ) as CaptureSpoolPartitionMetadata | undefined;
      if (partition?.oldestOrdinal === ordinal) {
        metadata.put({ ...partition, nextAttemptAtMs });
      }
    }
    await transactionDone(transaction);
  }

  async snapshot(partition: string, nowMs = Date.now()): Promise<CaptureSpoolSnapshot> {
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const expired = await pruneExpiredCaptureItems(chunks, metadata, nowMs);
    const snapshot = await readMetadataSnapshot(metadata, partition, expired);
    await transactionDone(transaction);
    return snapshot;
  }

  async listPartitions(nowMs = Date.now()): Promise<CaptureSpoolInventory> {
    const db = await this.database();
    const transaction = db.transaction(
      [CAPTURE_SPOOL_CHUNKS_STORE, CAPTURE_SPOOL_METADATA_STORE],
      "readwrite",
    );
    const chunks = transaction.objectStore(CAPTURE_SPOOL_CHUNKS_STORE);
    const metadata = transaction.objectStore(CAPTURE_SPOOL_METADATA_STORE);
    const expired = await pruneExpiredCaptureItems(chunks, metadata, nowMs);
    const values = await requestResult(metadata.getAll()) as CaptureSpoolMetadata[];
    await transactionDone(transaction);
    const global = values.find(
      (value): value is CaptureSpoolGlobalMetadata => value.kind === "global",
    ) ?? emptyGlobalMetadata();
    const partitions = values
      .filter(
        (value): value is CaptureSpoolPartitionMetadata =>
          value.kind === "partition" && value.depth > 0,
      )
      .map(({ key: _key, kind: _kind, ...partition }) => partition)
      .sort((left, right) => left.oldestOrdinal - right.oldestOrdinal);
    return {
      partitions,
      globalDepth: global.depth,
      globalBytes: global.bytes,
      expired,
    };
  }
}

export class MemoryCaptureUploadSpool implements CaptureUploadSpool {
  private items: CaptureSpoolItem[];
  private nextOrdinal: number;

  constructor(seed: CaptureSpoolItem[] = []) {
    this.items = seed.map((item) => ({ ...item }));
    this.nextOrdinal = Math.max(0, ...this.items.map((item) => item.ordinal ?? 0)) + 1;
  }

  private prune(nowMs: number): number {
    const before = this.items.length;
    this.items = this.items.filter((item) => item.expiresAtMs > nowMs);
    return before - this.items.length;
  }

  private current(partition: string, expired: number): CaptureSpoolSnapshot {
    const partitionItems = this.items.filter(
      (item) => item.partition === partition,
    );
    return {
      depth: partitionItems.length,
      bytes: partitionItems.reduce((sum, item) => sum + item.byteSize, 0),
      globalDepth: this.items.length,
      globalBytes: this.items.reduce((sum, item) => sum + item.byteSize, 0),
      partitionCount: new Set(this.items.map((item) => item.partition)).size,
      expired,
    };
  }

  async enqueue(
    item: CaptureSpoolItem,
    nowMs = Date.now(),
    options: CaptureSpoolEnqueueOptions = {},
  ): Promise<CaptureSpoolEnqueueResult> {
    validItem(item);
    const expired = this.prune(nowMs);
    const snapshot = this.current(item.partition, expired);
    const rejection = capacityRejection(
      snapshot,
      item,
      options.activePartition ?? item.partition,
    );
    if (rejection) {
      return { accepted: false, reason: rejection, snapshot };
    }
    this.items.push({ ...item, ordinal: this.nextOrdinal++ });
    return {
      accepted: true,
      snapshot: {
        depth: snapshot.depth + 1,
        bytes: snapshot.bytes + item.byteSize,
        globalDepth: snapshot.globalDepth + 1,
        globalBytes: snapshot.globalBytes + item.byteSize,
        partitionCount: snapshot.partitionCount + (snapshot.depth === 0 ? 1 : 0),
        expired,
      },
    };
  }

  async peek(partition: string, nowMs = Date.now()) {
    const result = await this.peekBatch(partition, 1, nowMs);
    return { item: result.items[0] ?? null, snapshot: result.snapshot };
  }

  async peekBatch(partition: string, limit: number, nowMs = Date.now()) {
    const expired = this.prune(nowMs);
    const scoped = this.items
      .filter((item) => item.partition === partition)
      .sort((left, right) => (left.ordinal ?? 0) - (right.ordinal ?? 0));
    return {
      items: scoped.slice(0, normalizedBatchLimit(limit)),
      snapshot: this.current(partition, expired),
    };
  }

  async acknowledge(ordinal: number): Promise<void> {
    this.items = this.items.filter((item) => item.ordinal !== ordinal);
  }

  async markRetry(ordinal: number, retryCount: number, nextAttemptAtMs: number): Promise<void> {
    this.items = this.items.map((item) =>
      item.ordinal === ordinal ? { ...item, retryCount, nextAttemptAtMs } : item,
    );
  }

  async snapshot(partition: string, nowMs = Date.now()): Promise<CaptureSpoolSnapshot> {
    return (await this.peek(partition, nowMs)).snapshot;
  }

  async listPartitions(nowMs = Date.now()): Promise<CaptureSpoolInventory> {
    const expired = this.prune(nowMs);
    const partitions = new Map<string, CaptureSpoolPartitionSummary>();
    for (const item of [...this.items].sort(
      (left, right) => (left.ordinal ?? 0) - (right.ordinal ?? 0),
    )) {
      const ordinal = item.ordinal ?? 0;
      const aggregate = partitions.get(item.partition);
      if (aggregate) {
        aggregate.depth += 1;
        aggregate.bytes += item.byteSize;
      } else {
        partitions.set(item.partition, {
          partition: item.partition,
          origin: item.origin,
          principalScope: item.principalScope,
          deviceId: item.scope.deviceId,
          captureSessionId: item.scope.captureSessionId,
          depth: 1,
          bytes: item.byteSize,
          oldestOrdinal: ordinal,
          oldestCapturedAtMs: item.capturedAtMs,
          nextAttemptAtMs: item.nextAttemptAtMs,
        });
      }
    }
    const snapshot = this.current("", expired);
    return {
      partitions: [...partitions.values()],
      globalDepth: snapshot.globalDepth,
      globalBytes: snapshot.globalBytes,
      expired,
    };
  }
}

export function captureSpoolPartition(
  origin: string,
  principalScope: string,
  deviceId: string,
  captureSessionId: string,
  lane: CaptureSpoolLane = "free",
): string {
  if (!captureSessionId) {
    throw new Error("capture session is required for a v2 spool partition");
  }
  return JSON.stringify([
    lane === "formal" ? "v2-formal" : "v2",
    origin,
    principalScope,
    deviceId,
    captureSessionId,
  ]);
}

/**
 * 只用于识别持久化在升级前 IndexedDB 中的 v1 分区。
 * 新 enqueue 必须调用 captureSpoolPartition 写入 v2 key。
 */
export function captureSpoolLegacyPartition(
  origin: string,
  principalScope: string,
  deviceId: string,
): string {
  return JSON.stringify([origin, principalScope, deviceId]);
}

/**
 * 只接受当前 scope 下、带 capture session 的新 generation 分区。
 * 旧 v1 key 已在持久 cutover 中删除，且永远不得被 recovery 重新导入。
 */
export function isCaptureSpoolPartitionCompatible(
  partition: string,
  origin: string,
  principalScope: string,
  deviceId: string,
  captureSessionId?: string,
): boolean {
  return Boolean(
    captureSessionId &&
      (partition ===
        captureSpoolPartition(
          origin,
          principalScope,
          deviceId,
          captureSessionId,
        ) ||
        partition ===
          captureSpoolPartition(
            origin,
            principalScope,
            deviceId,
            captureSessionId,
            "formal",
          )),
  );
}

function normalizedBatchLimit(limit: number): number {
  if (!Number.isFinite(limit)) return 1;
  return Math.max(1, Math.floor(limit));
}

export function captureRetryDelay(retryCount: number): number {
  const index = Math.min(
    CAPTURE_RETRY_BACKOFF_MS.length - 1,
    Math.max(0, retryCount - 1),
  );
  return CAPTURE_RETRY_BACKOFF_MS[index];
}

/**
 * Resolve one durable-upload retry delay from the bounded server signal.
 * `asr_no_eligible_provider` is a provider-wide condition, not a property of
 * one chunk; a one-second per-item retry would turn a backlog into a request
 * storm. Retry-After remains authoritative when it asks for a longer delay.
 */
export function captureUploadRetryDelay(
  error: unknown,
  retryCount: number,
): number {
  const candidate = error as {
    status?: unknown;
    errorClass?: unknown;
    retryAfterMs?: unknown;
  } | null;
  const retryAfterMs = candidate && typeof candidate.retryAfterMs === "number" &&
    Number.isFinite(candidate.retryAfterMs) && candidate.retryAfterMs > 0
    ? candidate.retryAfterMs
    : null;
  if (
    candidate?.status === 503 &&
    candidate.errorClass === "asr_no_eligible_provider"
  ) {
    return Math.max(CAPTURE_SYSTEMIC_FAILURE_BACKOFF_MS, retryAfterMs ?? 0);
  }
  return retryAfterMs ?? captureRetryDelay(retryCount);
}

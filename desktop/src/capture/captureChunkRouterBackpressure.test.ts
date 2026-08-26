import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./captureChunkRouter.ts", import.meta.url), "utf8");
const spoolSource = readFileSync(
  new URL("./captureUploadSpool.ts", import.meta.url),
  "utf8",
);

test("503 admission is decoupled from the durable spool and does not use a four-item drop gate", () => {
  assert.doesNotMatch(source, /ENQUEUE_STAGING_MAX_ITEMS/);
  assert.doesNotMatch(source, /ENQUEUE_STAGING_MAX_BYTES/);
  assert.match(source, /let enqueueTail: Promise<void> = Promise\.resolve\(\)/);
  assert.match(source, /const result = await captureUploadSpool\.enqueue\(/);
  assert.match(source, /if \(!result\.accepted\) \{/);
  assert.match(source, /isCaptureSpoolHardCapacityRejection\(result\.reason\)/);
  assert.match(source, /recordBackpressureLoss\(1\);/);
  assert.equal(source.match(/recordBackpressureLoss\(1\);/g)?.length, 1);
  assert.doesNotMatch(
    source,
    /onDiscarded:[\s\S]{0,160}recordBackpressureLoss/,
  );
  assert.match(source, /onDiscarded:[\s\S]{0,160}markUploadFailure/);
  assert.match(source, /onExpired:[\s\S]{0,120}expiredItems \+= count/);
});

test("durable capacity remains bounded by the exported durable limits", () => {
  assert.match(source, /CAPTURE_SPOOL_MAX_ITEMS/);
  assert.match(source, /CAPTURE_SPOOL_MAX_ITEMS/);
  assert.match(source, /only an explicit durable capacity rejection is backpressure/i);
});

test("a durable receipt from a recovery partition restores transport health", () => {
  const acknowledgeBody = source.slice(
    source.indexOf("const acknowledgeUpload"),
    source.indexOf("const processUploadResult"),
  );
  assert.match(acknowledgeBody, /observeDurableCaptureAcknowledgement/);
  assert.doesNotMatch(acknowledgeBody, /isActivePartition/);
  assert.doesNotMatch(source, /failedOrdinals|unscopedFailure/);
});

test("runtime diagnostics expose aggregate concurrency and latency only", () => {
  for (const field of [
    "activeInFlightCurrent",
    "activeInFlightMax",
    "recoveryInFlightCurrent",
    "recoveryInFlightMax",
    "globalInFlightCurrent",
    "globalInFlightMax",
    "attemptCount",
    "acknowledgedCount",
    "completedRequestCount",
    "requestLatencyMsSum",
    "requestLatencyMsMax",
    "enqueued",
    "lastEnqueueRejectReason",
    "activePartitionItemCount",
    "activePartitionByteCount",
    "activePartitionMaxItems",
    "activePartitionMaxBytes",
    "globalItemCount",
    "globalByteCount",
    "globalMaxItems",
    "globalMaxBytes",
    "partitionCount",
  ]) {
    assert.match(source, new RegExp(`${field}:`));
  }
  const diagnosticsBody = source.slice(
    source.indexOf("const diagnostics = () => ({"),
    source.indexOf("if (typeof window", source.indexOf("const diagnostics = () => ({")),
  );
  for (const field of [
    "lastEnqueueRejectReason",
    "activePartitionItemCount",
    "activePartitionByteCount",
    "activePartitionMaxItems",
    "activePartitionMaxBytes",
    "globalItemCount",
    "globalByteCount",
    "globalMaxItems",
    "globalMaxBytes",
    "partitionCount",
  ]) {
    assert.match(diagnosticsBody, new RegExp(`\\b${field}\\b`));
  }
  assert.doesNotMatch(
    diagnosticsBody,
    /currentPartition|partitionKey|origin|principalScope|deviceId|captureSessionId|segmentId|idempotencyKey|wav|scope/,
  );
  assert.match(
    source,
    /let lastEnqueueRejectReason: CaptureSpoolHardCapacityRejectReason \| null = null/,
  );
  assert.match(source, /lastEnqueueRejectReason = result\.reason/);
  assert.match(source, /lastEnqueueRejectReason = null/);
});

test("新写入只构造包含 capture session 的 v2 partition，旧 key 不得进入 recovery", () => {
  assert.match(source, /resolveSpoolTarget\([\s\S]{0,180}captureSessionId/);
  assert.match(
    source,
    /captureSpoolPartition\([\s\S]{0,160}deviceId,[\s\S]{0,80}captureSessionId/,
  );
  assert.match(spoolSource, /captureSpoolLegacyPartition/);
  assert.match(spoolSource, /CAPTURE_SPOOL_CUTOVER_GENERATION/);
  assert.match(spoolSource, /ensureCaptureSpoolGeneration/);
  assert.match(spoolSource, /chunks\.clear\(\)/);
  const validationBody = spoolSource.slice(
    spoolSource.indexOf("function validItem"),
    spoolSource.indexOf("function requestResult"),
  );
  assert.match(validationBody, /captureSpoolPartition\(/);
  assert.doesNotMatch(validationBody, /captureSpoolLegacyPartition/);
});

test("recovery fence 校验 owner/device，且允许本机 backend 重启换端口", () => {
  const recoveryEligibility = source.slice(
    source.indexOf("isPartitionEligible: async"),
    source.indexOf("onInventory:", source.indexOf("isPartitionEligible: async")),
  );
  assert.match(
    recoveryEligibility,
    /isCaptureSpoolOriginCompatible\(summary\.origin, target\.origin\)/,
  );
  assert.match(recoveryEligibility, /target\.principalScope === summary\.principalScope/);
  assert.match(recoveryEligibility, /target\.deviceId === summary\.deviceId/);
  assert.match(source, /target\.deviceId !== item\.scope\.deviceId/);
  assert.match(source, /scope: \{ \.\.\.item\.scope, captureSessionId \}/);
  assert.match(source, /expectedOrigin: target\.origin/);
  assert.match(source, /captureSpoolOrigin/);
});

test("formal capture enters a separate durable lane so free backlog cannot own every active slot", () => {
  const targetBody = source.slice(
    source.indexOf("const resolveSpoolTarget = async"),
    source.indexOf("const persistChunk = async"),
  );
  assert.match(targetBody, /captureSpoolPartition\([\s\S]{0,260}lane/);
  assert.match(
    source,
    /resolveSpoolTarget\([\s\S]{0,260}meetingId === null \? "free" : "formal"/,
  );
});

test("IndexedDB hot path uses reconciled metadata and bounded partition indexes", () => {
  assert.match(spoolSource, /reconcileCaptureSpoolMetadata/);
  assert.match(spoolSource, /CAPTURE_SPOOL_METADATA_STORE/);
  assert.match(spoolSource, /readPartitionBatch/);
  assert.match(spoolSource, /pruneExpiredCaptureItems/);
  const indexedClass = spoolSource.slice(
    spoolSource.indexOf("export class IndexedDbCaptureUploadSpool"),
    spoolSource.indexOf("export class MemoryCaptureUploadSpool"),
  );
  assert.doesNotMatch(indexedClass, /scanSpool|\.openCursor\(\)/);
});

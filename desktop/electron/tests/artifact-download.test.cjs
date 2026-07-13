"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  MAX_ARTIFACT_DOWNLOAD_BYTES,
  createDownloadTarget,
  downloadRendererBlob,
  sanitizeSuggestedFilename,
  uniqueDownloadFilename,
  validateBlobUrl,
} = require("../artifact-download.cjs");

const OBJECT_ID = "44fe282e-6daa-4226-92e7-8a046a1e8a92";
const APP_ORIGIN = "echodesk://app";
const BLOB_URL = `blob:${APP_ORIGIN}/${OBJECT_ID}`;

function temporaryDownloads(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-download-policy-"));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function frame(id = 11) {
  return { processId: 7, routingId: id, frameTreeNodeId: id + 100 };
}

function downloadHarness({
  totalBytes,
  bytes = Buffer.alloc(totalBytes > 0 ? totalBytes : 0),
  state = "completed",
  receivedBytes = totalBytes,
  emitUpdated = false,
  emittedUrl = BLOB_URL,
  partialCrdownload = false,
}) {
  const downloadSession = new EventEmitter();
  const mainFrame = frame();
  const item = new EventEmitter();
  let savePath = null;
  let cancelled = false;
  let prevented = false;
  item.getURL = () => emittedUrl;
  item.getTotalBytes = () => totalBytes;
  item.getReceivedBytes = () => receivedBytes;
  item.setSavePath = (target) => {
    savePath = target;
  };
  item.cancel = () => {
    cancelled = true;
  };
  const sender = {
    session: downloadSession,
    mainFrame,
    isDestroyed: () => false,
    downloadURL: (url, options) => {
      assert.equal(url, BLOB_URL);
      assert.equal(options, undefined);
      queueMicrotask(() => {
        const event = {
          prevented: false,
          preventDefault() {
            this.prevented = true;
            prevented = true;
          },
        };
        downloadSession.emit("will-download", event, item, sender);
        if (event.prevented || !savePath) return;
        fs.writeFileSync(partialCrdownload ? `${savePath}.crdownload` : savePath, bytes, {
          mode: 0o600,
        });
        if (emitUpdated) item.emit("updated", {}, "progressing");
        if (!cancelled) item.emit("done", {}, state);
      });
    },
  };
  return {
    sender,
    senderFrame: { ...mainFrame },
    item,
    wasCancelled: () => cancelled,
    wasPrevented: () => prevented,
  };
}

function request(downloadDirectory, harness, overrides = {}) {
  return downloadRendererBlob({
    blobUrl: BLOB_URL,
    expectedInnerOrigin: APP_ORIGIN,
    suggestedFilename: "report.txt",
    sender: harness.sender,
    senderFrame: harness.senderFrame,
    downloadDirectory,
    startTimeoutMs: 100,
    completionTimeoutMs: 100,
    ...overrides,
  });
}

test("blob policy binds a Chromium object URL to the exact renderer origin", () => {
  assert.equal(validateBlobUrl(BLOB_URL, APP_ORIGIN), BLOB_URL);
  assert.equal(
    validateBlobUrl(`blob:https://localhost:5174/${OBJECT_ID}`, "https://localhost:5174"),
    `blob:https://localhost:5174/${OBJECT_ID}`,
  );
  for (const value of [
    `blob:https://evil.example/${OBJECT_ID}`,
    `blob:null/${OBJECT_ID}`,
    "blob:foo",
    "https://app.example/artifact",
    `blob:${APP_ORIGIN}/not-an-object-id`,
  ]) {
    assert.throws(
      () => validateBlobUrl(value, APP_ORIGIN),
      (error) => error.code === "ARTIFACT_DOWNLOAD_URL_FORBIDDEN",
    );
  }
  assert.throws(
    () => validateBlobUrl(`${BLOB_URL}\n`, APP_ORIGIN),
    (error) => error.code === "ARTIFACT_DOWNLOAD_INPUT_INVALID",
  );
});

test("suggested filenames are basenames, portable, bounded, and non-reserved", () => {
  assert.equal(sanitizeSuggestedFilename("../../private/notes.txt"), "notes.txt");
  assert.equal(sanitizeSuggestedFilename("..\\..\\private\\notes.txt"), "notes.txt");
  assert.equal(sanitizeSuggestedFilename("bad:<name>?.txt"), "bad--name--.txt");
  assert.equal(sanitizeSuggestedFilename("CON.txt"), "echodesk-CON.txt");
  assert.equal(sanitizeSuggestedFilename("lpt9"), "echodesk-lpt9");
  assert.equal(sanitizeSuggestedFilename("..."), "echodesk-artifact");
  assert.ok(sanitizeSuggestedFilename("a".repeat(500)).length <= 180);
  assert.equal(uniqueDownloadFilename("report.txt", "abcdef"), "report-abcdef.txt");
});

test("download targets are generated only as fresh direct children", (t) => {
  const downloads = temporaryDownloads(t);
  const target = createDownloadTarget(downloads, "../report.txt");
  assert.equal(path.dirname(target.target), fs.realpathSync.native(downloads));
  assert.match(target.filename, /^report-[0-9a-f]{12}\.txt$/);
  assert.equal(fs.existsSync(target.target), false);
});

test("bounded renderer blob download writes a real file and returns no path", async (t) => {
  const downloads = temporaryDownloads(t);
  const marker = Buffer.from("ECHODESK_AUTHENTICATED_DOWNLOAD_OK", "utf8");
  const harness = downloadHarness({ totalBytes: marker.length, bytes: marker });
  const result = await request(downloads, harness);
  assert.deepEqual(result, {
    ok: true,
    cancelled: false,
    filename: result.filename,
  });
  assert.equal(path.isAbsolute(result.filename), false);
  assert.match(result.filename, /^report-[0-9a-f]{12}\.txt$/);
  const downloadedPath = path.join(downloads, result.filename);
  assert.deepEqual(fs.readFileSync(downloadedPath), marker);
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(downloadedPath).mode & 0o777, 0o600);
  }
});

test("zero-byte downloads are valid and materialize an empty file", async (t) => {
  const downloads = temporaryDownloads(t);
  const result = await request(downloads, downloadHarness({ totalBytes: 0 }));
  assert.equal(result.ok, true);
  assert.equal(fs.statSync(path.join(downloads, result.filename)).size, 0);
});

test("unknown and oversized downloads fail closed without partial files", async (t) => {
  for (const totalBytes of [-1, Number.NaN, MAX_ARTIFACT_DOWNLOAD_BYTES + 1]) {
    const downloads = temporaryDownloads(t);
    const harness = downloadHarness({ totalBytes, bytes: Buffer.alloc(0) });
    await assert.rejects(
      request(downloads, harness),
      (error) =>
        error.code === "ARTIFACT_DOWNLOAD_SIZE_UNKNOWN" ||
        error.code === "ARTIFACT_DOWNLOAD_TOO_LARGE",
    );
    assert.deepEqual(fs.readdirSync(downloads), []);
  }
});

test("received-byte overflow cancels and deletes the partial file", async (t) => {
  const downloads = temporaryDownloads(t);
  const harness = downloadHarness({
    totalBytes: 4,
    bytes: Buffer.from("12345"),
    receivedBytes: 5,
    emitUpdated: true,
    partialCrdownload: true,
  });
  await assert.rejects(
    request(downloads, harness, { maxBytes: 4 }),
    (error) => error.code === "ARTIFACT_DOWNLOAD_TOO_LARGE",
  );
  assert.equal(harness.wasCancelled(), true);
  assert.deepEqual(fs.readdirSync(downloads), []);
});

test("user cancellation is non-error and deletes the partial file", async (t) => {
  const downloads = temporaryDownloads(t);
  const harness = downloadHarness({
    totalBytes: 4,
    bytes: Buffer.from("1234"),
    state: "cancelled",
    partialCrdownload: true,
  });
  const result = await request(downloads, harness);
  assert.deepEqual(result, { ok: false, cancelled: true, filename: null });
  assert.deepEqual(fs.readdirSync(downloads), []);
});

test("subframes and unmatched blob events cannot claim the download", async (t) => {
  const downloads = temporaryDownloads(t);
  const subframeHarness = downloadHarness({ totalBytes: 1 });
  subframeHarness.senderFrame = frame(99);
  assert.throws(
    () => request(downloads, subframeHarness),
    (error) => error.code === "ARTIFACT_DOWNLOAD_SENDER_INVALID",
  );

  const unmatched = downloadHarness({
    totalBytes: 1,
    emittedUrl: `blob:${APP_ORIGIN}/11111111-1111-4111-8111-111111111111`,
  });
  await assert.rejects(
    request(downloads, unmatched, { startTimeoutMs: 10 }),
    (error) => error.code === "ARTIFACT_DOWNLOAD_URL_FORBIDDEN",
  );
  assert.equal(unmatched.wasPrevented(), true);
  assert.equal(unmatched.sender.session.listenerCount("will-download"), 0);
  assert.deepEqual(fs.readdirSync(downloads), []);
});

test("oversized filename IPC input is rejected before starting a download", (t) => {
  const downloads = temporaryDownloads(t);
  const harness = downloadHarness({ totalBytes: 1 });
  assert.throws(
    () => request(downloads, harness, { suggestedFilename: "a".repeat(513) }),
    (error) => error.code === "ARTIFACT_DOWNLOAD_INPUT_INVALID",
  );
  assert.equal(harness.sender.session.listenerCount("will-download"), 0);
  assert.deepEqual(fs.readdirSync(downloads), []);
});

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomBytes } = require("node:crypto");

const MAX_ARTIFACT_DOWNLOAD_BYTES = 128 * 1024 * 1024;
const MAX_BLOB_URL_LENGTH = 4096;
const MAX_RAW_FILENAME_LENGTH = 512;
const MAX_SUGGESTED_FILENAME_LENGTH = 180;
const DOWNLOAD_START_TIMEOUT_MS = 15_000;
const DOWNLOAD_COMPLETION_TIMEOUT_MS = 120_000;

class ArtifactDownloadError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "ArtifactDownloadError";
    this.code = code;
  }
}

function artifactDownloadError(code) {
  const messages = {
    ARTIFACT_DOWNLOAD_INPUT_INVALID: "artifact download input is invalid",
    ARTIFACT_DOWNLOAD_URL_FORBIDDEN: "artifact download URL is forbidden",
    ARTIFACT_DOWNLOAD_SENDER_INVALID: "artifact download sender is invalid",
    ARTIFACT_DOWNLOAD_DIRECTORY_UNAVAILABLE: "artifact download directory is unavailable",
    ARTIFACT_DOWNLOAD_START_TIMEOUT: "artifact download did not start",
    ARTIFACT_DOWNLOAD_SIZE_UNKNOWN: "artifact download size is unknown",
    ARTIFACT_DOWNLOAD_TOO_LARGE: "artifact download exceeds the size limit",
    ARTIFACT_DOWNLOAD_INTERRUPTED: "artifact download was interrupted",
  };
  return new ArtifactDownloadError(messages[code] || "artifact download failed", code);
}

function validateBlobUrl(rawUrl, expectedInnerOrigin) {
  if (
    typeof rawUrl !== "string" ||
    rawUrl.length === 0 ||
    rawUrl.length > MAX_BLOB_URL_LENGTH ||
    /[\u0000-\u001f\u007f]/.test(rawUrl) ||
    typeof expectedInnerOrigin !== "string" ||
    expectedInnerOrigin.length === 0 ||
    expectedInnerOrigin.endsWith("/")
  ) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_INPUT_INVALID");
  }
  let candidate;
  try {
    candidate = new URL(rawUrl);
  } catch {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_URL_FORBIDDEN");
  }
  const objectId = rawUrl.slice(`blob:${expectedInnerOrigin}/`.length);
  if (
    candidate.protocol !== "blob:" ||
    candidate.href !== rawUrl ||
    !rawUrl.startsWith(`blob:${expectedInnerOrigin}/`) ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      objectId,
    )
  ) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_URL_FORBIDDEN");
  }
  return candidate.href;
}

function sanitizeSuggestedFilename(rawName) {
  const input = typeof rawName === "string" ? rawName.normalize("NFKC") : "";
  const basename = path.basename(input.replaceAll("\\", "/"));
  const cleaned = basename
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .replace(/[<>:"/\\|?*]/g, "-")
    .replace(/[. ]+$/g, "")
    .trim()
    .slice(0, MAX_SUGGESTED_FILENAME_LENGTH);
  if (!cleaned || cleaned === "." || cleaned === "..") return "echodesk-artifact";
  const extension = path.extname(cleaned);
  const stem = cleaned.slice(0, cleaned.length - extension.length);
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(stem)) {
    return `echodesk-${cleaned}`;
  }
  return cleaned;
}

function uniqueDownloadFilename(suggestedFilename, randomSuffix) {
  const safeName = sanitizeSuggestedFilename(suggestedFilename);
  const extension = path.extname(safeName);
  const stem = safeName.slice(0, safeName.length - extension.length) || "echodesk-artifact";
  const suffix = randomSuffix || randomBytes(6).toString("hex");
  return `${stem}-${suffix}${extension}`;
}

function ensureDownloadDirectory(downloadDirectory) {
  if (typeof downloadDirectory !== "string" || !path.isAbsolute(downloadDirectory)) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_DIRECTORY_UNAVAILABLE");
  }
  try {
    fs.mkdirSync(downloadDirectory, { recursive: true, mode: 0o700 });
    const stat = fs.statSync(downloadDirectory);
    if (!stat.isDirectory()) throw new Error("not a directory");
    return fs.realpathSync.native(downloadDirectory);
  } catch {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_DIRECTORY_UNAVAILABLE");
  }
}

function createDownloadTarget(downloadDirectory, suggestedFilename) {
  const realDirectory = ensureDownloadDirectory(downloadDirectory);
  const filename = uniqueDownloadFilename(suggestedFilename);
  const target = path.join(realDirectory, filename);
  if (path.dirname(target) !== realDirectory || fs.existsSync(target)) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_DIRECTORY_UNAVAILABLE");
  }
  return { filename, target };
}

function downloadRendererBlob({
  blobUrl,
  expectedInnerOrigin,
  suggestedFilename,
  sender,
  senderFrame,
  downloadDirectory,
  maxBytes = MAX_ARTIFACT_DOWNLOAD_BYTES,
  startTimeoutMs = DOWNLOAD_START_TIMEOUT_MS,
  completionTimeoutMs = DOWNLOAD_COMPLETION_TIMEOUT_MS,
}) {
  const trustedBlobUrl = validateBlobUrl(blobUrl, expectedInnerOrigin);
  if (
    suggestedFilename !== undefined &&
    (typeof suggestedFilename !== "string" ||
      suggestedFilename.length > MAX_RAW_FILENAME_LENGTH)
  ) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_INPUT_INVALID");
  }
  const mainFrame = sender?.mainFrame;
  if (
    !sender ||
    sender.isDestroyed?.() === true ||
    !sender.session ||
    !mainFrame ||
    !senderFrame ||
    senderFrame.processId !== mainFrame.processId ||
    senderFrame.routingId !== mainFrame.routingId ||
    senderFrame.frameTreeNodeId !== mainFrame.frameTreeNodeId ||
    typeof sender.downloadURL !== "function"
  ) {
    throw artifactDownloadError("ARTIFACT_DOWNLOAD_SENDER_INVALID");
  }
  const { filename, target } = createDownloadTarget(
    downloadDirectory,
    suggestedFilename,
  );

  return new Promise((resolve, reject) => {
    const downloadSession = sender.session;
    let settled = false;
    let matchedItem = null;
    let policyCancelled = false;
    let expectedBytes = null;
    let completionTimer = null;

    const removePartial = () => {
      for (const partial of [target, `${target}.crdownload`]) {
        try {
          fs.rmSync(partial, { force: true });
        } catch {
          // The renderer only receives a stable error code; never expose a local path.
        }
      }
    };

    const cleanup = () => {
      clearTimeout(startTimer);
      if (completionTimer !== null) clearTimeout(completionTimer);
      downloadSession.removeListener("will-download", onWillDownload);
      if (matchedItem) {
        matchedItem.removeListener("updated", onUpdated);
        matchedItem.removeListener("done", onDone);
      }
    };
    const finish = (callback) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback();
    };
    const rejectCode = (code) =>
      finish(() => {
        removePartial();
        reject(artifactDownloadError(code));
      });
    const onUpdated = (_event, state) => {
      if (state === "interrupted") {
        policyCancelled = true;
        matchedItem?.cancel();
        rejectCode("ARTIFACT_DOWNLOAD_INTERRUPTED");
        return;
      }
      if (matchedItem && matchedItem.getReceivedBytes() > maxBytes) {
        policyCancelled = true;
        matchedItem.cancel();
        rejectCode("ARTIFACT_DOWNLOAD_TOO_LARGE");
      }
    };
    const onDone = (_event, state) => {
      if (state === "completed") {
        try {
          const stat = fs.lstatSync(target);
          if (
            !stat.isFile() ||
            !Number.isSafeInteger(expectedBytes) ||
            stat.size !== expectedBytes ||
            stat.size > maxBytes
          ) {
            rejectCode("ARTIFACT_DOWNLOAD_INTERRUPTED");
            return;
          }
          fs.chmodSync(target, 0o600);
        } catch {
          rejectCode("ARTIFACT_DOWNLOAD_INTERRUPTED");
          return;
        }
        finish(() => resolve({ ok: true, cancelled: false, filename }));
      } else if (state === "cancelled" && !policyCancelled) {
        finish(() => {
          removePartial();
          resolve({ ok: false, cancelled: true, filename: null });
        });
      } else {
        rejectCode("ARTIFACT_DOWNLOAD_INTERRUPTED");
      }
    };
    const onWillDownload = (event, item, webContents) => {
      if (webContents !== sender) return;
      if (item.getURL() !== trustedBlobUrl) {
        event.preventDefault();
        rejectCode("ARTIFACT_DOWNLOAD_URL_FORBIDDEN");
        return;
      }
      const totalBytes = item.getTotalBytes();
      if (!Number.isSafeInteger(totalBytes) || totalBytes < 0) {
        event.preventDefault();
        rejectCode("ARTIFACT_DOWNLOAD_SIZE_UNKNOWN");
        return;
      }
      if (totalBytes > maxBytes) {
        event.preventDefault();
        rejectCode("ARTIFACT_DOWNLOAD_TOO_LARGE");
        return;
      }
      clearTimeout(startTimer);
      matchedItem = item;
      expectedBytes = totalBytes;
      item.setSavePath(target);
      item.on("updated", onUpdated);
      item.once("done", onDone);
      completionTimer = setTimeout(() => {
        policyCancelled = true;
        matchedItem?.cancel();
        rejectCode("ARTIFACT_DOWNLOAD_INTERRUPTED");
      }, completionTimeoutMs);
    };
    const startTimer = setTimeout(
      () => rejectCode("ARTIFACT_DOWNLOAD_START_TIMEOUT"),
      startTimeoutMs,
    );

    downloadSession.on("will-download", onWillDownload);
    try {
      sender.downloadURL(trustedBlobUrl);
    } catch {
      rejectCode("ARTIFACT_DOWNLOAD_URL_FORBIDDEN");
    }
  });
}

module.exports = {
  ArtifactDownloadError,
  DOWNLOAD_COMPLETION_TIMEOUT_MS,
  DOWNLOAD_START_TIMEOUT_MS,
  MAX_ARTIFACT_DOWNLOAD_BYTES,
  MAX_BLOB_URL_LENGTH,
  MAX_RAW_FILENAME_LENGTH,
  MAX_SUGGESTED_FILENAME_LENGTH,
  createDownloadTarget,
  downloadRendererBlob,
  sanitizeSuggestedFilename,
  uniqueDownloadFilename,
  validateBlobUrl,
};

"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

function source(relative) {
  return readFileSync(path.resolve(__dirname, "../../src", relative), "utf8");
}

const download = source("components/AuthenticatedDownloadLink.tsx");
const preload = readFileSync(path.resolve(__dirname, "../preload.cjs"), "utf8");
const preview = source("components/ArtifactPreviewModal.tsx");
const panel = source("components/ArtifactPanel.tsx");
const minutes = source("components/MinutesView.tsx");
const transcript = source("components/TranscriptStream.tsx");

test("artifact links use authenticated bounded blob downloads", () => {
  assert.match(download, /apiTransport\(/);
  assert.match(download, /maxResponseBytes: AUTHENTICATED_DOWNLOAD_MAX_BYTES/);
  assert.match(download, /registerAbortController/);
  assert.match(download, /URL\.createObjectURL\(blob\)/);
  assert.match(download, /URL\.revokeObjectURL\(objectUrl\)/);
  assert.match(download, /bridge\?\.isElectron === true/);
  assert.match(download, /downloadArtifactBlob\(objectUrl, downloadName\)/);
  assert.match(download, /if \(result\.cancelled\) return/);
  assert.match(preload, /"echo:download-renderer-blob"/);
  assert.doesNotMatch(preload, /downloadArtifactBlob:[\s\S]{0,180}authorization|bearer/i);
  assert.match(download, /const anchor = document\.createElement\("a"\)/);
  for (const ui of [preview, panel, minutes, transcript]) {
    assert.doesNotMatch(ui, /href=\{artifactDownloadUrl\(/);
  }
});

test("HTML preview fetches through transport and renders only a sandboxed blob URL", () => {
  assert.match(
    preview,
    /AuthenticatedIframePreview[\s\S]+?apiTransport\([\s\S]+?URL\.createObjectURL\(blob\)/,
  );
  assert.match(preview, /sandbox=\{kind === "html" \? "" : undefined\}/);
  assert.match(preview, /src=\{state\.objectUrl\}/);
  assert.doesNotMatch(preview, /<iframe[\s\S]{0,120}src=\{downloadUrl\}/);
});

test("public PPT downloads retain blob URLs for a bounded grace and revoke on unload", () => {
  assert.match(preview, /PPTX_DOWNLOAD_OBJECT_URL_GRACE_MS = 30_000/);
  assert.match(preview, /activePptxDownloadUrls = new Map/);
  assert.match(preview, /addEventListener\("pagehide", revokeAllPptxDownloadUrls\)/);
  assert.match(preview, /addEventListener\("beforeunload", revokeAllPptxDownloadUrls\)/);
  assert.match(preview, /retainPptxDownloadUrl\(objectUrl\)/);
  assert.match(preview, /pendingObjectUrl && !objectUrlRetained/);
  assert.match(
    preview,
    /if \(!response\.ok\) \{[\s\S]{0,120}response\.body\?\.cancel\(\)[\s\S]{0,120}throw new Error/,
  );
  assert.doesNotMatch(
    preview,
    /setTimeout\(\(\) => URL\.revokeObjectURL\(objectUrl\), 0\)/,
  );
});

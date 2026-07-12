/* eslint-disable @typescript-eslint/no-var-requires */

const { readFileSync } = require("node:fs");

const FORBIDDEN_FROZEN_ENTRIES = [
  "speech_recognition",
  "flac-mac",
  "nvidia",
  "torch._dynamo",
  "torch._inductor",
  "triton",
];
const REQUIRED_CPU_DIARIZER_ENTRIES = [
  "speechbrain.inference.speaker",
  "torch",
  "torch.distributed",
  "torchaudio",
];

function containsCollectedModule(analysis, entry) {
  return new RegExp(
    `\\(\\s*["']${entry.replaceAll(".", "\\.")}(?:[./\\\\][^"']*)?["']\\s*,`,
    "i",
  ).test(analysis);
}

function containsForbiddenFrozenEntry(analysis, entry) {
  if (entry !== "flac-mac") {
    // Analysis-00.toc also serializes the explicit `excludes` array.  Match an
    // actual collected TOC tuple whose logical name is the package (or a child),
    // not the harmless exclusion declaration or Hugging Face's
    // `automatic_speech_recognition` module.
    return containsCollectedModule(analysis, entry);
  }
  return analysis.includes(entry);
}

function verifyFrozenAnalysis(analysisPath) {
  const analysis = readFileSync(analysisPath, "utf8").toLowerCase();
  const found = FORBIDDEN_FROZEN_ENTRIES.filter((entry) =>
    containsForbiddenFrozenEntry(analysis, entry),
  );
  if (found.length) {
    throw new Error(
      `[backend-build] forbidden optional or accelerator runtime in frozen manifest: ${found.join(", ")}`,
    );
  }
  const missing = REQUIRED_CPU_DIARIZER_ENTRIES.filter(
    (entry) => !containsCollectedModule(analysis, entry),
  );
  if (missing.length) {
    throw new Error(
      `[backend-build] frozen CPU diarizer runtime is incomplete: ${missing.join(", ")}`,
    );
  }
  return true;
}

module.exports = {
  FORBIDDEN_FROZEN_ENTRIES,
  REQUIRED_CPU_DIARIZER_ENTRIES,
  verifyFrozenAnalysis,
};

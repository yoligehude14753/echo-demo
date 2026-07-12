/* eslint-disable @typescript-eslint/no-var-requires */

const { readFileSync } = require("node:fs");

const FORBIDDEN_FROZEN_ENTRIES = ["speech_recognition", "flac-mac"];

function containsForbiddenFrozenEntry(analysis, entry) {
  if (entry === "speech_recognition") {
    // Analysis-00.toc also serializes the explicit `excludes` array.  Match an
    // actual collected TOC tuple whose logical name is the package (or a child),
    // not the harmless exclusion declaration or Hugging Face's
    // `automatic_speech_recognition` module.
    return /\(\s*["']speech_recognition(?:[./\\][^"']*)?["']\s*,/i.test(
      analysis,
    );
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
      `[backend-build] forbidden optional audio runtime in frozen manifest: ${found.join(", ")}`,
    );
  }
  return true;
}

module.exports = { FORBIDDEN_FROZEN_ENTRIES, verifyFrozenAnalysis };

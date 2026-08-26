/* eslint-disable @typescript-eslint/no-var-requires */

const { readFileSync } = require("node:fs");

const FORBIDDEN_FROZEN_ENTRIES = [
  "ddgs",
  "duckduckgo_search",
  "jieba",
  "rank_bm25",
  "speech_recognition",
  "flac-mac",
  "nvidia",
  "torch._dynamo",
  "torch._inductor",
  "triton",
];
const FORBIDDEN_LEGACY_APP_MODULES = [
  "app.adapters.rag.bm25",
  "app.adapters.rag.index_store",
  "app.adapters.stt.local",
  "app.adapters.stt.stepfun",
  "app.api.debug",
  "app.api.devices",
  "app.api.dream",
  "app.api.openai_compat",
  "app.api.profile",
  "app.api.soul",
  "app.api.system",
  "app.api.tasks",
  "app.asr",
  "app.dream",
  "app.llm",
  "app.metrics",
  "app.nodes.router",
  "app.persona",
  "app.pipeline",
  "app.s2s",
  "app.schemas.openai_compat",
  "app.session",
  "app.soul",
  "app.stt",
  "app.tasks",
  "app.tools.registry",
  "app.tools.task_manager",
  "app.tools.web_search",
  "app.ws",
  "app.ws_gateway",
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
  const legacyModules = FORBIDDEN_LEGACY_APP_MODULES.filter((entry) =>
    containsCollectedModule(analysis, entry),
  );
  if (legacyModules.length) {
    throw new Error(
      `[backend-build] legacy application path in frozen manifest: ${legacyModules.join(", ")}`,
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
  FORBIDDEN_LEGACY_APP_MODULES,
  REQUIRED_CPU_DIARIZER_ENTRIES,
  verifyFrozenAnalysis,
};

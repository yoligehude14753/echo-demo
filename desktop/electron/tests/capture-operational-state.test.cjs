"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");
const vm = require("node:vm");

function loadCaptureOperationalState() {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../../src/capture/captureOperationalState.ts"),
    "utf8",
  );
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: "captureOperationalState.ts",
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(output, {
    module,
    exports: module.exports,
    require,
    console,
    Date,
    Number,
    Object,
    Set,
    Array,
  });
  return module.exports;
}

function stats(overrides = {}) {
  return {
    stats_sequence: 5,
    chunks_total: 10,
    stored: 10,
    gated_rms: 10,
    gated_low_speech: 0,
    accepted_speech_frames: 10,
    observed_audio_frames: 10,
    last_gate_reason: "rms_too_low",
    last_chunk_at: "2026-07-14T09:00:00.000Z",
    ...overrides,
  };
}

test("backend generation reset rebuilds freshness and admission baselines", () => {
  const {
    createCaptureAdmissionState,
    createCaptureFreshnessState,
    observeCaptureAdmission,
    observeCaptureStatsFailure,
    observeCaptureStatsSuccess,
  } = loadCaptureOperationalState();
  const first = stats();
  const reset = stats({
    stats_sequence: 1,
    chunks_total: 1,
    stored: 1,
    gated_rms: 0,
    accepted_speech_frames: 1,
    observed_audio_frames: 1,
    last_gate_reason: "ok",
    last_chunk_at: "2026-07-14T09:01:00.000Z",
  });

  let freshness = observeCaptureStatsSuccess(
    createCaptureFreshnessState(),
    first,
    1_000,
  );
  freshness = observeCaptureStatsFailure(freshness);
  freshness = observeCaptureStatsFailure(freshness);
  assert.equal(freshness.warning, "stats_unavailable");
  const admission = observeCaptureAdmission(
    createCaptureAdmissionState(),
    null,
    first,
  );
  assert.equal(admission.warning, "rms_too_low");

  const nextFreshness = observeCaptureStatsSuccess(freshness, reset, 2_000);
  const nextAdmission = observeCaptureAdmission(admission, first, reset);
  assert.deepEqual(
    {
      warning: nextFreshness.warning,
      lastSequence: nextFreshness.lastSequence,
      lastTimestamp: nextFreshness.lastTimestamp,
      lastFreshAt: nextFreshness.lastFreshAt,
      admissionWarning: nextAdmission.warning,
    },
    {
      warning: "none",
      lastSequence: 1,
      lastTimestamp: "2026-07-14T09:01:00.000Z",
      lastFreshAt: 2_000,
      admissionWarning: "none",
    },
  );
});

test("a reset with an old timestamp does not clear either warning axis", () => {
  const {
    createCaptureAdmissionState,
    createCaptureFreshnessState,
    observeCaptureAdmission,
    observeCaptureStatsFailure,
    observeCaptureStatsSuccess,
  } = loadCaptureOperationalState();
  const first = stats();
  const oldReset = stats({
    stats_sequence: 1,
    chunks_total: 1,
    stored: 1,
    gated_rms: 0,
    accepted_speech_frames: 1,
    observed_audio_frames: 1,
    last_gate_reason: "ok",
    last_chunk_at: "2026-07-14T08:59:00.000Z",
  });

  let freshness = observeCaptureStatsSuccess(
    createCaptureFreshnessState(),
    first,
    1_000,
  );
  freshness = observeCaptureStatsFailure(observeCaptureStatsFailure(freshness));
  const admission = observeCaptureAdmission(
    createCaptureAdmissionState(),
    null,
    first,
  );
  const nextFreshness = observeCaptureStatsSuccess(freshness, oldReset, 2_000);
  const nextAdmission = observeCaptureAdmission(admission, first, oldReset);
  assert.equal(nextFreshness.warning, "stats_unavailable");
  assert.equal(nextFreshness.lastSequence, 5);
  assert.equal(nextAdmission.warning, "rms_too_low");
});

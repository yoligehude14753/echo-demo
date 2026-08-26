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
    gated_stationary_noise: 0,
    accepted_speech_frames: 10,
    observed_audio_frames: 10,
    last_gate_reason: "rms_too_low",
    last_chunk_at: "2026-07-14T09:00:00.000Z",
    ...overrides,
  };
}

test("capture transport exposes the 8192-item durable spool capacity", () => {
  const {
    CAPTURE_QUEUE_CAPACITY,
    createCaptureTransportState,
  } = loadCaptureOperationalState();
  assert.equal(CAPTURE_QUEUE_CAPACITY, 8_192);
  assert.equal(createCaptureTransportState().queueCapacity, 8_192);
});

test("stationary noise observation is visible and clears on real speech", () => {
  const {
    createCaptureAdmissionState,
    observeCaptureAdmission,
  } = loadCaptureOperationalState();
  const baseline = stats({
    stats_sequence: 10,
    chunks_total: 10,
    gated_rms: 0,
    last_gate_reason: "ok",
  });
  const rejected = stats({
    stats_sequence: 11,
    chunks_total: 11,
    gated_rms: 0,
    gated_stationary_noise: 1,
    last_gate_reason: "stationary_noise",
  });
  const accepted = stats({
    stats_sequence: 12,
    chunks_total: 12,
    stored: 11,
    gated_rms: 0,
    gated_stationary_noise: 1,
    accepted_speech_frames: 20,
    observed_audio_frames: 20,
    last_gate_reason: "ok",
  });

  const warning = observeCaptureAdmission(
    createCaptureAdmissionState(),
    baseline,
    rejected,
  );
  assert.equal(warning.warning, "stationary_noise");

  const recovered = observeCaptureAdmission(warning, rejected, accepted);
  assert.equal(recovered.warning, "none");
});

test("any durable receipt clears a stale upload-unavailable warning", () => {
  const {
    createCaptureTransportState,
    observeDurableCaptureAcknowledgement,
  } = loadCaptureOperationalState();
  const failed = {
    ...createCaptureTransportState(),
    sent: 97,
    acknowledged: 90,
    consecutiveFailures: 3,
    warning: "upload_unavailable",
  };

  const recovered = observeDurableCaptureAcknowledgement(failed, { now: 12_345 });

  assert.equal(recovered.warning, "none");
  assert.equal(recovered.consecutiveFailures, 0);
  assert.equal(recovered.lastSuccessfulUploadAt, 12_345);
  assert.equal(recovered.sent, 97);
  assert.equal(recovered.acknowledged, 90);
});

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

test("speech detection expires when no new backend admission arrives", () => {
  const {
    CAPTURE_ADMISSION_RECENT_MS,
    createCaptureAdmissionState,
    hasRecentAcceptedSpeech,
    observeCaptureAdmission,
  } = loadCaptureOperationalState();
  const accepted = stats({
    stats_sequence: 10,
    chunks_total: 10,
    stored: 10,
    gated_rms: 0,
    last_gate_reason: "ok",
  });
  const admission = observeCaptureAdmission(
    createCaptureAdmissionState(),
    null,
    accepted,
    1_000,
  );
  const unchanged = observeCaptureAdmission(
    admission,
    accepted,
    accepted,
    6_000,
  );

  assert.equal(hasRecentAcceptedSpeech(unchanged, 1_000 + CAPTURE_ADMISSION_RECENT_MS), true);
  assert.equal(
    hasRecentAcceptedSpeech(unchanged, 1_001 + CAPTURE_ADMISSION_RECENT_MS),
    false,
  );
  assert.equal(unchanged.lastObservedAt, 1_000);
});

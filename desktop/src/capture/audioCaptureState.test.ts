import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { AudioCaptureStateMachine } from "./audioCaptureState.ts";

test("capture lifecycle is standby then initializing then capturing then standby", () => {
  const lifecycle = new AudioCaptureStateMachine();
  const observed: string[] = [];
  const off = lifecycle.subscribe((snapshot) => observed.push(snapshot.state));

  assert.equal(lifecycle.getSnapshot().state, "standby");
  const generation = lifecycle.begin();
  assert.equal(typeof generation, "number");
  assert.equal(lifecycle.getSnapshot().state, "initializing");
  assert.equal(lifecycle.markCapturing(generation!), true);
  assert.equal(lifecycle.getSnapshot().state, "capturing");
  lifecycle.stop();
  assert.deepEqual(lifecycle.getSnapshot(), {
    state: "standby",
    errorMessage: null,
    lastErrorCode: null,
    revision: 3,
  });
  assert.deepEqual(observed, [
    "standby",
    "initializing",
    "capturing",
    "standby",
  ]);
  off();
});

test("failed attempt can retry and restart without preserving an old error", () => {
  const lifecycle = new AudioCaptureStateMachine();
  const failedGeneration = lifecycle.begin()!;
  const retryGeneration = lifecycle.invalidateWithError(
    failedGeneration,
    "microphone initialization timeout",
  );

  assert.equal(typeof retryGeneration, "number");
  assert.deepEqual(
    {
      state: lifecycle.getSnapshot().state,
      code: lifecycle.getSnapshot().lastErrorCode,
    },
    { state: "error", code: "device" },
  );
  assert.equal(lifecycle.beginRetry(retryGeneration!), true);
  assert.equal(lifecycle.getSnapshot().errorMessage, null);
  assert.equal(lifecycle.markCapturing(retryGeneration!), true);
  lifecycle.stop();

  const restartedGeneration = lifecycle.begin();
  assert.notEqual(restartedGeneration, failedGeneration);
  assert.equal(lifecycle.getSnapshot().state, "initializing");
  assert.equal(lifecycle.getSnapshot().lastErrorCode, null);
});

test("stop during initialization makes its eventual callback stale", () => {
  const lifecycle = new AudioCaptureStateMachine();
  const stoppedGeneration = lifecycle.begin()!;
  lifecycle.stop();

  assert.equal(lifecycle.markCapturing(stoppedGeneration), false);
  assert.equal(
    lifecycle.invalidateWithError(stoppedGeneration, "stale callback"),
    null,
  );
  assert.equal(lifecycle.getSnapshot().state, "standby");
});

test("callbacks from a previous run cannot revive a restarted capture", () => {
  const lifecycle = new AudioCaptureStateMachine();
  const previousGeneration = lifecycle.begin()!;
  lifecycle.stop();
  const currentGeneration = lifecycle.begin()!;

  assert.equal(lifecycle.markCapturing(previousGeneration), false);
  assert.equal(lifecycle.getSnapshot().state, "initializing");
  assert.equal(lifecycle.markCapturing(currentGeneration), true);
  assert.equal(lifecycle.getSnapshot().state, "capturing");
});

test("UI projection and diagnostics both read AudioCapture's lifecycle snapshot", () => {
  const audioCaptureSource = readFileSync(
    new URL("./audioCapture.ts", import.meta.url),
    "utf8",
  );
  const hookSource = readFileSync(
    new URL("./useEchoCapture.ts", import.meta.url),
    "utf8",
  );

  assert.match(
    audioCaptureSource,
    /const lifecycle = this\.lifecycle\.getSnapshot\(\);[\s\S]*captureState: lifecycle\.state,[\s\S]*lastErrorCode: lifecycle\.lastErrorCode/,
  );
  assert.match(
    hookSource,
    /useState<AudioCaptureSnapshot>\([\s\S]*audioCapture\.getSnapshot\(\)/,
  );
  assert.match(
    hookSource,
    /audioCapture\.onStatus\(\(snapshot\) => \{[\s\S]*setCaptureSnapshot\(snapshot\)/,
  );
  assert.doesNotMatch(hookSource, /setCaptureState|setErrorMessage/);
  assert.doesNotMatch(
    audioCaptureSource,
    /private state: CaptureState = "initializing"|setState\("initializing"\)/,
  );
  assert.match(
    audioCaptureSource,
    /stop\(\): void \{[\s\S]*this\.voiceChunker\.finish\(\);[\s\S]*this\.lifecycle\.stop\(\);[\s\S]*this\.teardown\(\);/,
  );
  assert.match(
    audioCaptureSource,
    /proc\.onaudioprocess = \(ev\) => \{\s*if \(!this\.isCurrent\(generation\)\) return;/,
  );
  assert.match(
    audioCaptureSource,
    /selectedTrack\.addEventListener\("ended",[\s\S]*failWebAudioRuntime/,
  );
  assert.match(
    audioCaptureSource,
    /lastAudioCallbackAt = Date\.now\(\);[\s\S]*beginRuntimeWatchdog\(generation\)/,
  );
  assert.match(
    audioCaptureSource,
    /WEB_AUDIO_CALLBACK_STALE_MS[\s\S]*麦克风音频流已中断/,
  );
});

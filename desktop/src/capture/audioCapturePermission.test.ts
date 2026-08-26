import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const source = readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "audioCapture.ts"),
  "utf8",
);

test("renderer preflights Electron microphone access before getUserMedia", () => {
  assert.match(source, /async function requestElectronMicAccess\(\): Promise<void>/);
  assert.match(source, /settleElectronMicIpc\(window\.echo\?\.getMicStatus\)/);
  assert.match(source, /status === "granted"/);
  assert.match(source, /settleElectronMicIpc\(window\.echo\?\.requestMic\)/);
  assert.match(source, /ELECTRON_MIC_PREFLIGHT_TIMEOUT_MS = 3_000/);
  assert.match(
    source,
    /await requestElectronMicAccess\(\);[\s\S]*?let audioInputs = await listAudioInputDevices\(\)/,
  );
  assert.match(source, /getUserMediaWithTimeout\(/);
  assert.match(source, /navigator\.mediaDevices\s*\.getUserMedia/);
});

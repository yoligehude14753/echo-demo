import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync(new URL("../api.ts", import.meta.url), "utf8");
const routerSource = readFileSync(new URL("./captureChunkRouter.ts", import.meta.url), "utf8");

test("capture HTTP errors preserve only the machine-readable class", () => {
  assert.match(apiSource, /public readonly errorClass: string \| null = null/);
  assert.match(apiSource, /r\.headers\.get\("X-Capture-Error-Class"\)/);
  assert.match(apiSource, /new CaptureUploadHttpError\(r\.status, retryAfterMs, errorClass\)/);
  assert.match(routerSource, /error\.errorClass \?\? error\.name/);
});

test("only an explicit terminal meeting result is acknowledged without reactivating the meeting", () => {
  assert.match(apiSource, /"terminal_ignored"/);
  assert.match(routerSource, /if \(result\.stt_status === "terminal_ignored"\)/);
  const terminalBody = routerSource.slice(
    routerSource.indexOf('if (result.stt_status === "terminal_ignored")'),
    routerSource.indexOf("if (result.ambient_stored", routerSource.indexOf('if (result.stt_status === "terminal_ignored")')),
  );
  assert.doesNotMatch(terminalBody, /markMeetingActive/);
  assert.doesNotMatch(terminalBody, /stopFormalCaptureProducer/);
  assert.match(terminalBody, /releaseFormalMeetingPartitions/);
});

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../src/components/MinutesView.tsx"),
  "utf8",
);

test("minutes polling re-reads durable failure when websocket delivery is absent", () => {
  const missingMinutesBranch = source.slice(
    source.indexOf("if (!m)"),
    source.indexOf("const restoredMinutes"),
  );
  assert.match(missingMinutesBranch, /listMeetings\(/);
  assert.match(missingMinutesBranch, /minutes_status === "generation_failed"/);
  assert.match(missingMinutesBranch, /minutes_error: summary\.minutes_error/);
  assert.match(missingMinutesBranch, /return;/);
});

test("minutes polling and rendering treat no_content as a terminal non-error", () => {
  assert.match(source, /minutes_status === "no_content"/);
  assert.match(source, /没有足够的有效转写，未生成纪要/);
  const noContentReadback = source.slice(
    source.indexOf('summary?.minutes_status === "no_content"'),
    source.indexOf("scheduleRetry();", source.indexOf('summary?.minutes_status === "no_content"')),
  );
  assert.match(noContentReadback, /minutes_status: "no_content"/);
  assert.match(noContentReadback, /return;/);
  const noContentCard = source.slice(
    source.indexOf('meeting?.minutes_status === "no_content"'),
    source.indexOf("isFinalizedLike(meeting?.state)", source.indexOf('meeting?.minutes_status === "no_content"')),
  );
  assert.doesNotMatch(noContentCard, /onRetry/);
});

test("retry applies the returned minutes without waiting for websocket delivery", () => {
  const retryBody = source.slice(
    source.indexOf("const onRetry"),
    source.indexOf("const shareAction"),
  );
  assert.match(retryBody, /const minutes = await retryMinutesGeneration\(/);
  assert.match(retryBody, /upsertMeeting\(currentId, \{/);
  assert.match(retryBody, /minutes_status: "ok"/);
  assert.match(retryBody, /纪要已重新生成/);
});

test("free capture minutes copy matches automatic meeting finalization", () => {
  assert.match(source, /自由收音会自动识别完整对话并生成会议纪要/);
  assert.match(source, /也可点击下方按钮建立明确的正式会议边界/);
  assert.doesNotMatch(source, /自由收音不会自动生成会议纪要/);
});

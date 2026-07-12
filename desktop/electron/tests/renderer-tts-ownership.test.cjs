"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = readFileSync(
  path.resolve(__dirname, "../../src/hooks/useTtsPlayer.ts"),
  "utf8",
);

test("an old TTS generation cannot clear a newer source in finally", () => {
  assert.match(source, /sourceOwnerRef = useRef<\{/);
  assert.match(
    source,
    /sourceOwnerRef\.current = \{ source: src, controller, generation \}/,
  );
  assert.match(
    source,
    /currentOwner\.controller === controller[\s\S]+?currentOwner\.generation === generation/,
  );
  assert.doesNotMatch(
    source,
    /finally \{[\s\S]{0,160}sourceRef\.current = null;[\s\S]{0,80}setIsSpeaking\(false\)/,
  );
});

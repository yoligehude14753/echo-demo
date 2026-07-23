const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const script = fs.readFileSync(
  path.join(__dirname, "package-macos-source-assets.cjs"),
  "utf8",
);

test("source asset packager binds the frozen source SHA and performs offline readback", () => {
  assert.match(script, /--source-sha/);
  assert.match(script, /source_sha: sourceSha/);
  assert.match(script, /codesign.*--verify.*--deep.*--strict/);
  assert.match(script, /ditto.*--keepParent/);
  assert.match(script, /hdiutil.*create/);
  assert.match(script, /unzip.*-t/);
  assert.match(script, /hdiutil.*imageinfo/);
  assert.match(script, /SHA256SUMS/);
  assert.doesNotMatch(script, /curl|wget|https?:\/\//);
});

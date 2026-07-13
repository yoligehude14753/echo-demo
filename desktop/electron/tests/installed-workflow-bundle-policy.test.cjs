"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const workflow = readFileSync(
  path.resolve(__dirname, "../../tests/e2e-real/installed-local-workflow.spec.ts"),
  "utf8",
);

test("installed workflow launches the bundled backend and rejects source overrides", () => {
  assert.doesNotMatch(workflow, /ECHO_BACKEND_CWD:\s*BACKEND_ROOT/);
  assert.doesNotMatch(workflow, /ECHO_PYTHON:\s*PYTHON_BIN/);
  assert.match(workflow, /EXPECTED_BACKEND_BIN/);
  assert.match(workflow, /accessSync\(EXPECTED_BACKEND_BIN, constants\.X_OK\)/);
  assert.match(workflow, /descendantCommands\(electronPid \?\? -1\)/);
  assert.match(workflow, /command\.includes\(EXPECTED_BACKEND_BIN\)/);
  assert.match(workflow, /installed app must not launch source backend/);
  assert.match(
    workflow,
    /"ECHO_BACKEND_CWD",[\s\S]+?"ECHO_PYTHON",[\s\S]+?"ECHO_ALLOW_PACKAGED_SOURCE_BACKEND"/,
  );
  assert.match(workflow, /minutes_status === "generation_failed"/);
  assert.match(
    workflow,
    /postForm<JsonMap>\(first\.win, `\/meetings\/\$\{meetingId\}\/finalize`/,
  );
  assert.match(workflow, /waitForResponse\(/);
  assert.match(workflow, /app\.getPath\("downloads"\)/);
  assert.match(workflow, /stat\.isFile\(\)/);
  assert.match(workflow, /readFileSync\(candidate, "utf8"\)\.includes\(TODO_MARKER\)/);
  assert.match(workflow, /filename\.endsWith\("\.crdownload"\)/);
  assert.doesNotMatch(workflow, /__ECHODESK_DOWNLOAD_OBSERVATION__/);
  assert.doesNotMatch(workflow, /HTMLAnchorElement\.prototype\.click/);
  assert.doesNotMatch(
    workflow,
    /getByTestId\("minutes-todo-artifact-link"\)\)\.toHaveAttribute\(\s*"href"/,
  );
});

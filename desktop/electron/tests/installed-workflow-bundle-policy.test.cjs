"use strict";

const assert = require("node:assert/strict");
const { existsSync, readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "../..");
const repoRoot = path.resolve(desktopRoot, "..");

test("retired installed Playwright workflow stays absent", () => {
  assert.equal(
    existsSync(
      path.join(desktopRoot, "tests/e2e-real/installed-local-workflow.spec.ts"),
    ),
    false,
  );
});
test("installed acceptance is owned by the canonical zero-argument runner", () => {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const runnerPath = path.join(desktopRoot, "scripts/core-e2e-acceptance.cjs");
  assert.equal(existsSync(runnerPath), true);
  assert.equal(pkg.scripts["acceptance:core"], "node scripts/core-e2e-acceptance.cjs");
  assert.equal(pkg.scripts["test:core-e2e"], "node --test scripts/core-e2e-acceptance.test.cjs");

  const versionCheck = readFileSync(
    path.join(desktopRoot, "scripts/check-version-sync.cjs"),
    "utf8",
  );
  assert.doesNotMatch(versionCheck, /installed-local-workflow\.spec\.ts/);

  const canonical = readFileSync(
    path.join(desktopRoot, "scripts/canonical-only-contract.cjs"),
    "utf8",
  );
  assert.match(canonical, /desktop\/tests\/e2e-real\/installed-local-workflow\.spec\.ts/);
  assert.equal(
    existsSync(path.join(repoRoot, "desktop/electron/main.cjs")),
    true,
  );
});

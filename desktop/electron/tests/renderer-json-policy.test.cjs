"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = readFileSync(path.resolve(__dirname, "../../src/session.ts"), "utf8");

test("bounded bootstrap and identity JSON parsing replaces raw SyntaxErrors", () => {
  assert.match(source, /async function parseBoundedJsonResponse/);
  assert.match(source, /throw new Error\(`\$\{label\} response is invalid JSON`\)/);
  assert.doesNotMatch(
    source,
    /const body = \(await response\.json\(\)\) as Partial<BackendBootstrap>/,
  );
  assert.doesNotMatch(
    source,
    /return \(await response\.json\(\)\) as IssuedSessionResponse/,
  );
  assert.match(
    source,
    /parseBoundedJsonResponse<Partial<CredentialRotationResult>>/,
  );
});

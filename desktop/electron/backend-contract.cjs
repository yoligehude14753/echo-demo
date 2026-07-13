"use strict";

const { createHash } = require("node:crypto");
const {
  createReadStream,
  lstatSync,
  readFileSync,
  readdirSync,
} = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const path = require("node:path");

const BACKEND_PRODUCT_ID = "com.echodesk.app.backend";
const DESKTOP_API_CONTRACT = "echodesk.desktop-backend/v1";
const BUILD_CONTRACT_SCHEMA_VERSION = 1;
const BOOTSTRAP_SCHEMA_VERSION = 1;
const BOOTSTRAP_API_VERSION = "0.3";
const MAX_BOOTSTRAP_BYTES = 64 * 1024;
const SOURCE_SUFFIXES = new Set([".py", ".sql"]);
const REQUIRED_LOCAL_CAPABILITIES = Object.freeze({
  principal_sessions: true,
  owner_isolation: true,
  workflow_kernel: "dispatcher-v1",
  ws_owner_filtering: true,
  ws_stream_epoch: true,
  ws_hello_bearer: false,
  server_resync_rehydrate_required: true,
  host_runtime_requires_admin: false,
});

class BackendContractError extends Error {
  constructor(code) {
    super(`backend contract rejected: ${code}`);
    this.name = "BackendContractError";
    this.code = code;
  }
}

function sourceFiles(root) {
  const files = [];
  const visit = (directory) => {
    for (const name of readdirSync(directory).sort()) {
      const candidate = path.join(directory, name);
      const stat = lstatSync(candidate);
      if (stat.isSymbolicLink()) continue;
      if (stat.isDirectory()) {
        visit(candidate);
      } else if (stat.isFile() && SOURCE_SUFFIXES.has(path.extname(name))) {
        files.push(candidate);
      }
    }
  };
  visit(root);
  return files.sort((left, right) => {
    const leftRelative = path.relative(root, left).split(path.sep).join("/");
    const rightRelative = path.relative(root, right).split(path.sep).join("/");
    if (leftRelative < rightRelative) return -1;
    if (leftRelative > rightRelative) return 1;
    return 0;
  });
}

function sourceTreeBuildId(root) {
  const digest = createHash("sha256");
  for (const file of sourceFiles(root)) {
    const relative = path.relative(root, file).split(path.sep).join("/");
    digest.update(relative, "utf8");
    digest.update("\0");
    digest.update(readFileSync(file));
    digest.update("\0");
  }
  return `sha256:${digest.digest("hex")}`;
}

function fileBuildId(file) {
  return new Promise((resolve, reject) => {
    const digest = createHash("sha256");
    const stream = createReadStream(file);
    stream.on("data", (chunk) => digest.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve(`sha256:${digest.digest("hex")}`));
  });
}

function sourceCatalogMax(sourceAppPath) {
  const directory = path.join(sourceAppPath, "adapters", "repo", "migrations");
  const versions = readdirSync(directory)
    .map((name) => /^(\d{3,})_[A-Za-z0-9_-]+\.sql$/.exec(name))
    .filter(Boolean)
    .map((match) => Number(match[1]));
  return Math.max(0, ...versions);
}

async function expectedBackendContract({
  productVersion,
  bundledBackendPath = null,
  sourceAppPath = null,
}) {
  if (typeof productVersion !== "string" || !productVersion.trim()) {
    throw new BackendContractError("expected-version-missing");
  }
  if (!bundledBackendPath && !sourceAppPath) {
    throw new BackendContractError("expected-build-missing");
  }
  return Object.freeze({
    schema_version: BUILD_CONTRACT_SCHEMA_VERSION,
    product_id: BACKEND_PRODUCT_ID,
    product_version: productVersion,
    api_contract: DESKTOP_API_CONTRACT,
    build_id: bundledBackendPath
      ? await fileBuildId(bundledBackendPath)
      : sourceTreeBuildId(sourceAppPath),
    schema_catalog_max: sourceAppPath ? sourceCatalogMax(sourceAppPath) : null,
  });
}

function readBootstrap(baseUrl, { timeoutMs = 10_000 } = {}) {
  return new Promise((resolve, reject) => {
    let target;
    try {
      target = new URL("/bootstrap", baseUrl);
    } catch {
      reject(new BackendContractError("endpoint-invalid"));
      return;
    }
    if (!new Set(["http:", "https:"]).has(target.protocol)) {
      reject(new BackendContractError("endpoint-invalid"));
      return;
    }
    const transport = target.protocol === "https:" ? https : http;
    const request = transport.get(
      target,
      {
        timeout: timeoutMs,
        headers: { Accept: "application/json" },
      },
      (response) => {
        if (response.statusCode !== 200) {
          response.resume();
          reject(new BackendContractError("bootstrap-unavailable"));
          return;
        }
        const chunks = [];
        let received = 0;
        response.on("data", (chunk) => {
          received += chunk.length;
          if (received > MAX_BOOTSTRAP_BYTES) {
            request.destroy(new BackendContractError("bootstrap-too-large"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
          } catch {
            reject(new BackendContractError("bootstrap-invalid-json"));
          }
        });
      },
    );
    request.on("error", (error) => reject(
      error instanceof BackendContractError
        ? error
        : new BackendContractError("bootstrap-unreachable"),
    ));
    request.on("timeout", () => {
      request.destroy(new BackendContractError("bootstrap-timeout"));
    });
  });
}

function validateBackendContract(bootstrap, expected) {
  if (!bootstrap || typeof bootstrap !== "object") {
    throw new BackendContractError("bootstrap-shape-mismatch");
  }
  if (
    bootstrap.schema_version !== BOOTSTRAP_SCHEMA_VERSION ||
    bootstrap.api_version !== BOOTSTRAP_API_VERSION
  ) {
    throw new BackendContractError("api-version-mismatch");
  }
  if (
    bootstrap.backend_version !== expected.product_version ||
    bootstrap.app_version !== expected.product_version
  ) {
    throw new BackendContractError("product-version-mismatch");
  }
  const actual = bootstrap.build_contract;
  if (!actual || typeof actual !== "object") {
    throw new BackendContractError("build-contract-missing");
  }
  for (const key of [
    "schema_version",
    "product_id",
    "product_version",
    "api_contract",
    "build_id",
  ]) {
    if (actual[key] !== expected[key]) {
      throw new BackendContractError(`${key.replaceAll("_", "-")}-mismatch`);
    }
  }
  if (
    expected.schema_catalog_max !== null &&
    actual.schema_catalog_max !== expected.schema_catalog_max
  ) {
    throw new BackendContractError("schema-catalog-mismatch");
  }
  if (!Number.isSafeInteger(actual.schema_catalog_max) || actual.schema_catalog_max < 1) {
    throw new BackendContractError("schema-catalog-invalid");
  }
  const capabilities = bootstrap.capabilities;
  if (!capabilities || typeof capabilities !== "object") {
    throw new BackendContractError("capabilities-missing");
  }
  for (const [name, required] of Object.entries(REQUIRED_LOCAL_CAPABILITIES)) {
    if (capabilities[name] !== required) {
      throw new BackendContractError(`capability-${name}-mismatch`);
    }
  }
  return actual;
}

async function probeBackendContract(baseUrl, expected, options) {
  const bootstrap = await readBootstrap(baseUrl, options);
  return validateBackendContract(bootstrap, expected);
}

module.exports = {
  BACKEND_PRODUCT_ID,
  BUILD_CONTRACT_SCHEMA_VERSION,
  BackendContractError,
  DESKTOP_API_CONTRACT,
  REQUIRED_LOCAL_CAPABILITIES,
  expectedBackendContract,
  probeBackendContract,
  readBootstrap,
  sourceTreeBuildId,
  validateBackendContract,
};

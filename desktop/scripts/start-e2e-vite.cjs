#!/usr/bin/env node

"use strict";

const { execFileSync } = require("node:child_process");
const { mkdtempSync, readFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const port = Number.parseInt(process.argv[2] || "5174", 10);
if (!Number.isInteger(port) || port < 1 || port > 65_535) {
  throw new Error("a valid E2E HTTPS port is required");
}

const root = mkdtempSync(join(tmpdir(), "echodesk-e2e-tls-"));
const keyPath = join(root, "localhost.key");
const certPath = join(root, "localhost.crt");
let server = null;
let closing = false;

async function close(exitCode = 0) {
  if (closing) return;
  closing = true;
  try {
    await server?.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
    process.exit(exitCode);
  }
}

async function main() {
  execFileSync(
    "openssl",
    [
      "req",
      "-x509",
      "-newkey",
      "rsa:2048",
      "-sha256",
      "-nodes",
      "-keyout",
      keyPath,
      "-out",
      certPath,
      "-days",
      "1",
      "-subj",
      "/CN=localhost",
      "-addext",
      "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ],
    { stdio: "ignore" },
  );
  const { createServer } = await import("vite");
  server = await createServer({
    root: process.cwd(),
    cacheDir: join(root, "vite-cache"),
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      https: {
        key: readFileSync(keyPath),
        cert: readFileSync(certPath),
      },
    },
  });
  await server.listen();
  console.log(`[e2e-vite] https://localhost:${port}`);
}

process.once("SIGINT", () => void close(0));
process.once("SIGTERM", () => void close(0));
process.once("uncaughtException", (error) => {
  console.error(error);
  void close(1);
});
process.once("unhandledRejection", (error) => {
  console.error(error);
  void close(1);
});

void main().catch((error) => {
  console.error(error);
  void close(1);
});

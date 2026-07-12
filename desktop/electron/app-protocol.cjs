"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const APP_SCHEME = "echodesk";
const APP_HOST = "app";
const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;
const APP_ENTRY_URL = `${APP_ORIGIN}/index.html`;

const CONTENT_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gif", "image/gif"],
  [".htm", "text/html; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".ico", "image/x-icon"],
  [".jpeg", "image/jpeg"],
  [".jpg", "image/jpeg"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".map", "application/json; charset=utf-8"],
  [".mjs", "text/javascript; charset=utf-8"],
  [".png", "image/png"],
  [".svg", "image/svg+xml"],
  [".txt", "text/plain; charset=utf-8"],
  [".wasm", "application/wasm"],
  [".webp", "image/webp"],
  [".woff", "font/woff"],
  [".woff2", "font/woff2"],
]);

function registerAppScheme(protocol) {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: APP_SCHEME,
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        corsEnabled: true,
        codeCache: true,
      },
    },
  ]);
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative === "" ||
    (!path.isAbsolute(relative) && !relative.startsWith(`..${path.sep}`) && relative !== "..")
  );
}

function parseAppAssetUrl(rawUrl) {
  let candidate;
  try {
    candidate = new URL(rawUrl);
  } catch {
    return null;
  }
  if (
    candidate.protocol !== `${APP_SCHEME}:` ||
    candidate.hostname !== APP_HOST ||
    candidate.port ||
    candidate.username ||
    candidate.password
  ) {
    return null;
  }

  let pathname;
  try {
    pathname = decodeURIComponent(candidate.pathname);
  } catch {
    return null;
  }
  if (pathname.includes("\0") || pathname.includes("\\")) return null;
  return pathname === "/" ? "/index.html" : pathname;
}

async function resolveAppAssetPath(distRoot, rawUrl) {
  const pathname = parseAppAssetUrl(rawUrl);
  if (!pathname) return null;

  let root;
  try {
    root = await fs.promises.realpath(path.resolve(distRoot));
  } catch {
    return null;
  }
  const requested = path.resolve(root, `.${pathname}`);
  if (!isInside(root, requested)) return null;

  let resolved;
  let stat;
  try {
    resolved = await fs.promises.realpath(requested);
    stat = await fs.promises.lstat(requested);
  } catch {
    return null;
  }
  if (!isInside(root, resolved) || !stat.isFile() || stat.isSymbolicLink()) {
    return null;
  }
  return resolved;
}

function responseHeaders(filePath, size) {
  const extension = path.extname(filePath).toLowerCase();
  return {
    "Cache-Control": extension === ".html" ? "no-store" : "public, max-age=31536000, immutable",
    "Content-Length": String(size),
    "Content-Type": CONTENT_TYPES.get(extension) ?? "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
  };
}

function createAppProtocolHandler({ distRoot, fileFetcher = null }) {
  const root = path.resolve(distRoot);
  return async function handleAppProtocol(request) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response(null, {
        status: 405,
        headers: { Allow: "GET, HEAD" },
      });
    }

    const filePath = await resolveAppAssetPath(root, request.url);
    if (!filePath) {
      return new Response("Not Found", {
        status: 404,
        headers: {
          "Content-Type": "text/plain; charset=utf-8",
          "X-Content-Type-Options": "nosniff",
        },
      });
    }

    try {
      const stat = await fs.promises.stat(filePath);
      const headers = responseHeaders(filePath, stat.size);
      if (request.method === "HEAD") {
        return new Response(null, { status: 200, headers });
      }
      if (fileFetcher) {
        // Electron's documented file-protocol path uses net.fetch(file://...).
        // Keep its streaming body: constructing a navigation response from a
        // fully buffered Node Buffer can leave Chromium reloads in ERR_ABORTED.
        const fileResponse = await fileFetcher(pathToFileURL(filePath).toString());
        if (!fileResponse.ok || fileResponse.body === null) throw new Error("asset read failed");
        return new Response(fileResponse.body, {
          status: 200,
          headers,
        });
      }
      const body = await fs.promises.readFile(filePath);
      return new Response(body, {
        status: 200,
        headers,
      });
    } catch {
      return new Response("Not Found", {
        status: 404,
        headers: {
          "Content-Type": "text/plain; charset=utf-8",
          "X-Content-Type-Options": "nosniff",
        },
      });
    }
  };
}

function installAppProtocol(protocol, distRoot, fileFetcher) {
  if (typeof fileFetcher !== "function") {
    throw new TypeError("Electron net.fetch is required for the app protocol");
  }
  protocol.handle(APP_SCHEME, createAppProtocolHandler({ distRoot, fileFetcher }));
}

module.exports = {
  APP_ENTRY_URL,
  APP_HOST,
  APP_ORIGIN,
  APP_SCHEME,
  createAppProtocolHandler,
  installAppProtocol,
  parseAppAssetUrl,
  registerAppScheme,
  resolveAppAssetPath,
};

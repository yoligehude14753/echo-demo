"use strict";

const fs = require("node:fs");
const path = require("node:path");

const APP_SCHEME = "echodesk";
const APP_HOST = "app";
const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;
const APP_ENTRY_URL = `${APP_ORIGIN}/index.html`;
const VITE_LEGACY_BOOTSTRAP_HASH =
  "'sha256-tQjf8gvb2ROOMapIxFvFAYBeUJ0v1HCbOcSmDNXGtDo='";

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

function productionContentSecurityPolicy(rawBackendBase = null) {
  const backendSources = [];
  if (rawBackendBase) {
    try {
      const backend = new URL(String(rawBackendBase));
      if (
        ["http:", "https:"].includes(backend.protocol) &&
        !backend.username &&
        !backend.password &&
        backend.pathname === "/" &&
        !backend.search &&
        !backend.hash
      ) {
        backendSources.push(backend.origin);
        const websocket = new URL(backend.origin);
        websocket.protocol = backend.protocol === "https:" ? "wss:" : "ws:";
        backendSources.push(websocket.origin);
      }
    } catch {
      // Invalid backend configuration is rejected elsewhere and grants no CSP source.
    }
  }
  const httpBackendSources = backendSources.filter(
    (source) => source.startsWith("http://") || source.startsWith("https://"),
  );
  return [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    `script-src 'self' ${VITE_LEGACY_BOOTSTRAP_HASH}`,
    "style-src 'self' 'unsafe-inline'",
    `connect-src 'self' ${backendSources.join(" ")}`.trim(),
    `img-src 'self' data: blob: ${httpBackendSources.join(" ")}`.trim(),
    "font-src 'self' data:",
    `media-src 'self' blob: ${httpBackendSources.join(" ")}`.trim(),
    `frame-src 'self' blob: ${httpBackendSources.join(" ")}`.trim(),
    "worker-src 'self' blob:",
    "form-action 'none'",
  ].join("; ");
}

function responseHeaders(filePath, size, backendBase = null) {
  const extension = path.extname(filePath).toLowerCase();
  const headers = {
    "Cache-Control": extension === ".html" ? "no-store" : "public, max-age=31536000, immutable",
    "Content-Length": String(size),
    "Content-Type": CONTENT_TYPES.get(extension) ?? "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
  };
  if (extension === ".html") {
    headers["Content-Security-Policy"] =
      productionContentSecurityPolicy(backendBase);
    headers["Cross-Origin-Opener-Policy"] = "same-origin";
    headers["Permissions-Policy"] =
      "camera=(), geolocation=(), microphone=(self)";
    headers["Referrer-Policy"] = "no-referrer";
    headers["X-Frame-Options"] = "DENY";
  }
  return headers;
}

function createAppProtocolHandler({
  distRoot,
  backendBase = null,
}) {
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
      const headers = responseHeaders(filePath, stat.size, backendBase);
      if (request.method === "HEAD") {
        return new Response(null, { status: 200, headers });
      }
      // The product protocol already owns the URL-to-file mapping above.  Do
      // not route the verified local path back through Electron's file://
      // network stack: a rejected file fetch was indistinguishable from a
      // missing renderer entry and surfaced as the production "Not Found"
      // page.  Reading this verified regular file keeps the renderer on the
      // echodesk:// origin and never exposes file:// to the renderer.
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

function installAppProtocol(
  protocol,
  distRoot,
  { backendBase = null } = {},
) {
  protocol.handle(
    APP_SCHEME,
    createAppProtocolHandler({ distRoot, backendBase }),
  );
}

module.exports = {
  APP_ENTRY_URL,
  APP_HOST,
  APP_ORIGIN,
  APP_SCHEME,
  createAppProtocolHandler,
  installAppProtocol,
  parseAppAssetUrl,
  productionContentSecurityPolicy,
  registerAppScheme,
  resolveAppAssetPath,
};

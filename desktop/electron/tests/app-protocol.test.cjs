"use strict";

const assert = require("node:assert/strict");
const {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
  existsSync,
} = require("node:fs");
const { realpath } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  APP_ENTRY_URL,
  APP_ORIGIN,
  APP_SCHEME,
  createAppProtocolHandler,
  installAppProtocol,
  parseAppAssetUrl,
  productionContentSecurityPolicy,
  registerAppScheme,
  resolveAppAssetPath,
} = require("../app-protocol.cjs");

function withDist(run) {
  const root = mkdtempSync(path.join(os.tmpdir(), "echodesk-app-protocol-"));
  const dist = path.join(root, "dist");
  mkdirSync(path.join(dist, "assets"), { recursive: true });
  writeFileSync(path.join(dist, "index.html"), "<main>EchoDesk</main>");
  writeFileSync(path.join(dist, "assets", "app-abc123.js"), "export default true;");
  return Promise.resolve(run({ root, dist })).finally(() => {
    rmSync(root, { recursive: true, force: true });
  });
}

test("app scheme is standard and secure without privileged bypasses", () => {
  let registered = null;
  registerAppScheme({
    registerSchemesAsPrivileged(schemes) {
      registered = schemes;
    },
  });

  assert.deepEqual(registered, [
    {
      scheme: "echodesk",
      privileges: {
        standard: true,
        secure: true,
        supportFetchAPI: true,
        corsEnabled: true,
        codeCache: true,
      },
    },
  ]);
  assert.equal(registered[0].privileges.bypassCSP, undefined);
  assert.equal(registered[0].privileges.allowServiceWorkers, undefined);
  assert.equal(registered[0].privileges.stream, undefined);
});

test("app asset URL accepts only the exact product origin", () => {
  assert.equal(APP_ORIGIN, "echodesk://app");
  assert.equal(APP_ENTRY_URL, "echodesk://app/index.html");
  assert.equal(parseAppAssetUrl(APP_ENTRY_URL), "/index.html");
  assert.equal(parseAppAssetUrl("echodesk://app/"), "/index.html");
  assert.equal(parseAppAssetUrl("echodesk://evil/index.html"), null);
  assert.equal(parseAppAssetUrl("echodesk://app.evil/index.html"), null);
  assert.equal(parseAppAssetUrl("echodesk://user@app/index.html"), null);
  assert.equal(parseAppAssetUrl("echodesk://app:443/index.html"), null);
  assert.equal(parseAppAssetUrl("file:///tmp/index.html"), null);
  assert.equal(parseAppAssetUrl("echodesk://app/%E0%A4%A"), null);
});

test("asset resolver contains decoded paths inside dist", async () => {
  await withDist(async ({ dist }) => {
    assert.equal(
      await resolveAppAssetPath(dist, APP_ENTRY_URL),
      await realpath(path.join(dist, "index.html")),
    );
    assert.equal(
      await resolveAppAssetPath(dist, "echodesk://app/assets/app-abc123.js"),
      await realpath(path.join(dist, "assets", "app-abc123.js")),
    );
    assert.equal(
      await resolveAppAssetPath(dist, "echodesk://app/%2F..%2Foutside.txt"),
      null,
    );
    assert.equal(
      await resolveAppAssetPath(dist, "echodesk://app/assets%5C..%5Cindex.html"),
      null,
    );
    assert.equal(await resolveAppAssetPath(dist, "echodesk://app/missing.txt"), null);
  });
});

test(
  "asset resolver rejects a symlink that escapes dist",
  { skip: process.platform === "win32" },
  async () => {
    await withDist(async ({ root, dist }) => {
      const secret = path.join(root, "secret.txt");
      writeFileSync(secret, "outside");
      symlinkSync(secret, path.join(dist, "assets", "outside.txt"));
      assert.equal(
        await resolveAppAssetPath(dist, "echodesk://app/assets/outside.txt"),
        null,
      );
    });
  },
);

test("protocol handler is read-only and serves controlled static responses", async () => {
  await withDist(async ({ dist }) => {
    const handler = createAppProtocolHandler({
      distRoot: dist,
      backendBase: "https://backend.example",
    });

    const index = await handler(new Request(APP_ENTRY_URL));
    assert.equal(index.status, 200);
    assert.equal(index.headers.get("content-type"), "text/html; charset=utf-8");
    assert.equal(index.headers.get("cache-control"), "no-store");
    assert.equal(index.headers.get("x-content-type-options"), "nosniff");
    const csp = index.headers.get("content-security-policy");
    assert.match(csp, /script-src 'self' 'sha256-/);
    assert.doesNotMatch(csp, /script-src[^;]*'unsafe-inline'/);
    assert.match(
      csp,
      /connect-src 'self' https:\/\/backend\.example wss:\/\/backend\.example/,
    );
    assert.match(csp, /object-src 'none'/);
    assert.match(csp, /frame-ancestors 'none'/);
    assert.equal(index.headers.get("referrer-policy"), "no-referrer");
    assert.equal(index.headers.get("x-frame-options"), "DENY");
    assert.equal(await index.text(), "<main>EchoDesk</main>");

    const asset = await handler(
      new Request("echodesk://app/assets/app-abc123.js", { method: "HEAD" }),
    );
    assert.equal(asset.status, 200);
    assert.equal(asset.headers.get("content-type"), "text/javascript; charset=utf-8");
    assert.match(asset.headers.get("cache-control"), /immutable/);
    assert.equal(asset.headers.get("content-length"), String("export default true;".length));
    assert.equal(asset.headers.get("content-security-policy"), null);
    assert.equal(await asset.text(), "");

    const writeAttempt = await handler(
      new Request(APP_ENTRY_URL, { method: "POST", body: "overwrite" }),
    );
    assert.equal(writeAttempt.status, 405);
    assert.equal(writeAttempt.headers.get("allow"), "GET, HEAD");

    const missing = await handler(new Request("echodesk://app/missing.txt"));
    assert.equal(missing.status, 404);
  });
});

test(
  "built renderer index and an emitted asset resolve while missing and traversal URLs stay rejected",
  { skip: process.env.ECHODESK_VERIFY_BUILT_PROTOCOL !== "1" },
  async () => {
    const dist = path.resolve(__dirname, "../../dist");
    assert.ok(existsSync(path.join(dist, "index.html")), "run npm run build before this check");
    const indexHtml = readFileSync(path.join(dist, "index.html"), "utf8");
    const assetPath = indexHtml.match(/(?:src|href)="\.\/(assets\/[^"?]+(?:\.js|\.css))"/)?.[1];
    assert.ok(assetPath, "built index must reference a hashed renderer asset");

    const handler = createAppProtocolHandler({ distRoot: dist });
    const index = await handler(new Request(APP_ENTRY_URL));
    assert.equal(index.status, 200);

    const asset = await handler(new Request(`${APP_ORIGIN}/${assetPath}`));
    assert.equal(asset.status, 200);
    assert.ok(Number(asset.headers.get("content-length")) > 0);

    assert.equal(
      (await handler(new Request(`${APP_ORIGIN}/missing.txt`))).status,
      404,
    );
    assert.equal(
      (await handler(new Request(`${APP_ORIGIN}/%2F..%2Fpackage.json`))).status,
      404,
    );
  },
);

test("production CSP grants only the bound backend and the fixed legacy bootstrap hash", () => {
  const csp = productionContentSecurityPolicy("http://127.0.0.1:8769");
  assert.match(csp, /connect-src 'self' http:\/\/127\.0\.0\.1:8769 ws:\/\/127\.0\.0\.1:8769/);
  assert.doesNotMatch(csp, /https:\s|wss:\s|\*/);
  assert.doesNotMatch(csp, /script-src[^;]*'unsafe-inline'/);

  const source = readFileSync(path.resolve(__dirname, "../../index.html"), "utf8");
  const boot = readFileSync(
    path.resolve(__dirname, "../../public/boot-fallback.js"),
    "utf8",
  );
  const vite = readFileSync(
    path.resolve(__dirname, "../../vite.config.ts"),
    "utf8",
  );
  assert.match(
    source,
    /<script vite-ignore src="\.\/boot-fallback\.js"><\/script>/,
  );
  assert.doesNotMatch(source, /<script>\s*\(function/);
  assert.doesNotMatch(boot, /innerHTML/);
  assert.match(boot, /textContent/);
  assert.match(vite, /echodesk-csp-inline-script-gate/);
  assert.match(vite, /CSP_INLINE_SCRIPT_HASHES/);
});

test("protocol installer binds only the product scheme and reads only verified build files", () => {
  let installed = null;
  installAppProtocol(
    {
      handle(scheme, handler) {
        installed = { scheme, handler };
      },
    },
    "/tmp/echodesk-dist",
  );
  assert.equal(installed.scheme, APP_SCHEME);
  assert.equal(typeof installed.handler, "function");
});

test("main process registers before ready and never falls back to file loading", () => {
  const main = readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  assert.ok(main.indexOf("registerAppScheme(protocol)") < main.indexOf("app.whenReady()"));
  assert.match(main, /installAppProtocol\(\s*protocol,/);
  assert.match(main, /\{ backendBase: BACKEND_HOST \}/);
  assert.match(main, /await mainWindow\.loadURL\(APP_ENTRY_URL\)/);
  assert.match(main, /error\?\.code === "ERR_ABORTED"/);
  assert.match(main, /isTrustedAppRendererUrl\(currentUrl\)/);
  assert.doesNotMatch(main, /mainWindow\.loadFile\(/);
  assert.doesNotMatch(main, /url\.startsWith\("file:\/\/"\)/);
  assert.doesNotMatch(main, /net\.fetch\(url\)/);
});

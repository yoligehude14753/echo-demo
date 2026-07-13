import { expect, test, _electron as electron } from "@playwright/test";
import { existsSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";

const APP_BIN = process.env.ECHODESK_APP_BIN ?? null;
const CLIENT_VERSION = JSON.parse(
  readFileSync(path.join(process.cwd(), "package.json"), "utf8"),
).version as string;
const PUBLIC_BACKEND_BASE = (
  process.env.ECHO_PUBLIC_BACKEND_BASE ?? "https://echodesk.yoliyoli.uk"
).replace(/\/+$/, "");
const TEST_USER_DATA =
  process.env.ECHODESK_TEST_USER_DATA ??
  path.join(process.env.TMPDIR ?? "/tmp", "echodesk-packaged-public-transport-e2e");

test("packaged public app uses the official Origin with anonymous health and session HTTP/WS", async () => {
  test.skip(!APP_BIN || !existsSync(APP_BIN), "ECHODESK_APP_BIN is required");
  test.setTimeout(90_000);

  rmSync(TEST_USER_DATA, { recursive: true, force: true });
  const args = [`--user-data-dir=${TEST_USER_DATA}`];
  if (process.env.ECHODESK_TEST_TLS_SPKI) {
    args.push(
      `--ignore-certificate-errors-spki-list=${process.env.ECHODESK_TEST_TLS_SPKI}`,
    );
  }
  const env = {
    ...process.env,
    ECHO_PUBLIC_DEMO: "1",
    ECHO_PUBLIC_BACKEND_BASE: PUBLIC_BACKEND_BASE,
  };
  delete env.ECHO_FORCE_LOCAL_BACKEND;
  delete env.ECHO_BACKEND_PORT;

  const app = await electron.launch({
    executablePath: APP_BIN!,
    cwd: path.dirname(APP_BIN!),
    args,
    env,
    timeout: 60_000,
  });

  try {
    const win = await app.firstWindow({ timeout: 60_000 });
    await win.waitForLoadState("domcontentloaded");
    expect(
      await win.evaluate(() => ({
        origin: window.location.origin,
        protocol: window.location.protocol,
        pathname: window.location.pathname,
      })),
    ).toEqual({
      origin: "echodesk://app",
      protocol: "echodesk:",
      pathname: "/index.html",
    });

    const metaRequests: Array<{ path: string; authorization: string }> = [];
    win.on("request", (request) => {
      const url = new URL(request.url());
      if (["/bootstrap", "/healthz", "/healthz/full"].includes(url.pathname)) {
        metaRequests.push({
          path: url.pathname,
          authorization: request.headers().authorization ?? "",
        });
      }
    });

    const result = await win.evaluate(async ({ expectedBase, clientVersion }) => {
      const base = await window.echo?.getBackendHost?.();
      if (base !== expectedBase) throw new Error(`unexpected backend: ${base}`);
      await document.fonts.ready;
      const legacyEntry = document.querySelector<HTMLScriptElement>(
        "#vite-legacy-entry[data-src]",
      );
      if (!legacyEntry?.dataset.src) throw new Error("legacy entry unavailable");
      const bundleUrl = new URL(legacyEntry.dataset.src, window.location.href).toString();
      const bundleResponse = await fetch(bundleUrl, { cache: "no-store" });
      const bundleBody = await bundleResponse.text();
      const injectedStyle = Array.from(document.querySelectorAll("style")).find((style) =>
        style.textContent?.includes("--ed-surface"),
      );
      if (!injectedStyle?.textContent) throw new Error("injected application CSS unavailable");
      const transcriptHeader = document.querySelector<HTMLElement>(
        ".echodesk-transcript-header",
      );
      const conversationTitle = document.querySelector<HTMLElement>(
        "[data-testid='conversation-mode-title']",
      );
      const transcriptA11y = document.querySelector<HTMLElement>(
        "[data-testid='transcript-title']",
      );
      if (!transcriptHeader || !conversationTitle || !transcriptA11y) {
        throw new Error("transcript layout contract unavailable");
      }
      const headerStyle = window.getComputedStyle(transcriptHeader);
      const conversationStyle = window.getComputedStyle(conversationTitle);
      const a11yStyle = window.getComputedStyle(transcriptA11y);
      const a11yRect = transcriptA11y.getBoundingClientRect();
      const [healthResponse, bootstrapResponse] = await Promise.all([
        fetch(`${base}/healthz`, { cache: "no-store", credentials: "omit" }),
        fetch(`${base}/bootstrap`, { cache: "no-store", credentials: "omit" }),
      ]);
      const health = (await healthResponse.json()) as Record<string, unknown>;
      const bootstrap = (await bootstrapResponse.json()) as Record<string, unknown>;

      const session = await window.echo?.ensurePublicSession?.();
      if (!session?.token) throw new Error("server-issued session unavailable");
      const meetings = await fetch(`${base}/meetings?limit=1`, {
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${session.token}`,
          "X-EchoDesk-Client-Version": clientVersion,
        },
      });

      const wsType = await new Promise<string>((resolve, reject) => {
        const socket = new WebSocket(`${base.replace(/^http/, "ws")}/ws/echo`);
        const timer = window.setTimeout(() => {
          socket.close();
          reject(new Error("public websocket handshake timeout"));
        }, 15_000);
        socket.onopen = () => {
          socket.send(
            JSON.stringify({
              type: "client_hello",
              last_seq: 0,
              client_version: clientVersion,
              auth: { type: "bearer", token: session.token },
            }),
          );
        };
        socket.onmessage = (event) => {
          const message = JSON.parse(String(event.data)) as { type?: string };
          if (message.type !== "server_hello") return;
          window.clearTimeout(timer);
          socket.close(1000, "transport E2E complete");
          resolve(message.type);
        };
        socket.onerror = () => {
          window.clearTimeout(timer);
          reject(new Error("public websocket handshake failed"));
        };
      });

      return {
        healthStatus: healthResponse.status,
        health,
        bootstrapStatus: bootstrapResponse.status,
        bootstrap,
        meetingsStatus: meetings.status,
        wsType,
        assets: {
          bundleStatus: bundleResponse.status,
          bundleContentType: bundleResponse.headers.get("content-type"),
          bundleBytes: bundleBody.length,
          bundleContainsCss: bundleBody.includes("--ed-surface"),
          injectedStyleBytes: injectedStyle.textContent.length,
          injectedStyleContainsSrOnly: injectedStyle.textContent.includes(".sr-only"),
          fontsStatus: document.fonts.status,
          bodyFontFamily: window.getComputedStyle(document.body).fontFamily,
          surfaceVariable: window
            .getComputedStyle(document.documentElement)
            .getPropertyValue("--ed-surface")
            .trim(),
          headerHeight: headerStyle.height,
          conversationHeight: conversationStyle.height,
          conversationFontSize: conversationStyle.fontSize,
          a11yText: transcriptA11y.textContent?.trim() ?? "",
          a11yWidth: a11yRect.width,
          a11yHeight: a11yRect.height,
          a11yPosition: a11yStyle.position,
          a11yOverflow: a11yStyle.overflow,
        },
      };
    }, { expectedBase: PUBLIC_BACKEND_BASE, clientVersion: CLIENT_VERSION });

    expect(result.healthStatus).toBe(200);
    expect(result.health).toEqual({ status: "ok" });
    expect(result.bootstrapStatus).toBe(200);
    expect(result.bootstrap.session_required).toBe(true);
    expect(result.bootstrap.minimum_client_version).toBe(CLIENT_VERSION);
    expect(result.bootstrap).not.toHaveProperty("backend_version");
    expect(result.meetingsStatus).toBe(200);
    expect(result.wsType).toBe("server_hello");
    expect(result.assets.bundleStatus).toBe(200);
    expect(result.assets.bundleContentType).toContain("text/javascript");
    expect(result.assets.bundleBytes).toBeGreaterThan(100_000);
    expect(result.assets.bundleContainsCss).toBe(true);
    expect(result.assets.injectedStyleBytes).toBeGreaterThan(10_000);
    expect(result.assets.injectedStyleContainsSrOnly).toBe(true);
    expect(result.assets.fontsStatus).toBe("loaded");
    expect(result.assets.bodyFontFamily).toContain("-apple-system");
    expect(result.assets.surfaceVariable).not.toBe("");
    expect(result.assets.headerHeight).toBe("48px");
    expect(Number.parseFloat(result.assets.conversationHeight)).toBeGreaterThanOrEqual(18);
    expect(Number.parseFloat(result.assets.conversationFontSize)).toBeGreaterThanOrEqual(12);
    expect(result.assets).toMatchObject({
      a11yText: "对话流",
      a11yWidth: 1,
      a11yHeight: 1,
      a11yPosition: "absolute",
      a11yOverflow: "hidden",
    });
    expect(metaRequests.some((request) => request.path === "/healthz/full")).toBe(false);
    expect(
      metaRequests
        .filter((request) => request.path === "/healthz")
        .every((request) => request.authorization === ""),
    ).toBe(true);
  } finally {
    await app.close();
  }
});

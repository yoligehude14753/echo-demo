/**
 * Electron 打包 App E2E（真发布版二进制）。
 *
 * 直接 launch 打包后的平台二进制：
 *   - 默认 macOS: /Applications/EchoDesk.app/Contents/MacOS/EchoDesk
 *   - Windows/Linux: 通过 ECHODESK_APP_BIN 指定 exe/AppImage/解包二进制
 * 让 Electron 自己 spawn 后端，验证：
 *   1. 主窗口启动不白屏（首个 BrowserWindow 加载完毕）
 *   2. 显式 public demo 模式走云端 backend，不要求本机 Python
 *   3. WS 与 backend 真握手成功（"已连接"）
 *   4. Settings / Workspace / CommandBar 核心点击路径可用
 *
 * 注意：
 * - 这是"打包后真二进制"路径，覆盖 vite dev 之外的 Electron 主进程 + preload + release 资源路径
 * - 未设置 ECHODESK_APP_BIN 时，非 macOS 会跳过
 */
import { test, expect, _electron as electron, type Page } from "@playwright/test";
import { existsSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";

function defaultAppBin(): string | null {
  if (process.env.ECHODESK_APP_BIN) return process.env.ECHODESK_APP_BIN;
  if (process.platform === "darwin") {
    return "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk";
  }
  return null;
}

const APP_BIN = defaultAppBin();
const CLIENT_VERSION = JSON.parse(
  readFileSync(path.join(process.cwd(), "package.json"), "utf8"),
).version as string;
const PUBLIC_BACKEND_BASE = (
  process.env.ECHO_PUBLIC_BACKEND_BASE ?? "https://echodesk.yoliyoli.uk"
).replace(/\/+$/, "");
const TEST_USER_DATA =
  process.env.ECHODESK_TEST_USER_DATA ??
  path.join(process.env.TMPDIR ?? "/tmp", "echodesk-packaged-public-e2e");

type RectInfo = {
  x: number;
  y: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
  fontSize: string;
  placeholderFontSize: string;
  display: string;
  visibility: string;
};

function requireRect(rect: RectInfo | null, name: string): RectInfo {
  expect(rect, `${name} should exist`).not.toBeNull();
  return rect!;
}

test.describe("EchoDesk 打包 App", () => {
  test.skip(!APP_BIN || !existsSync(APP_BIN), "未设置或找不到 ECHODESK_APP_BIN，跳过打包 App 测试");

  test("packaged app: 启动不白屏 + 显式 public backend + 核心交互可点击", async () => {
    test.setTimeout(180_000);
    const env = {
      ...process.env,
      ECHO_PUBLIC_DEMO: "1",
      ECHO_PUBLIC_BACKEND_BASE: PUBLIC_BACKEND_BASE,
    };
    delete env.ECHO_FORCE_LOCAL_BACKEND;
    delete env.ECHO_BACKEND_PORT;
    rmSync(TEST_USER_DATA, { recursive: true, force: true });
    const args = [`--user-data-dir=${TEST_USER_DATA}`];
    if (process.env.ECHODESK_TEST_TLS_SPKI) {
      args.push(
        `--ignore-certificate-errors-spki-list=${process.env.ECHODESK_TEST_TLS_SPKI}`,
      );
    }

    const app = await electron.launch({
      executablePath: APP_BIN!,
      cwd: path.dirname(APP_BIN!),
      args,
      env,
      timeout: 60_000,
    });

    try {
      // 第一个窗口 = 主 BrowserWindow
      const win: Page = await app.firstWindow({ timeout: 60_000 });
      // 等 React 渲染
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

      const publicMetaRequests: Array<{
        path: string;
        authorization: string;
      }> = [];
      win.on("request", (request) => {
        const url = new URL(request.url());
        if (["/bootstrap", "/healthz", "/healthz/full"].includes(url.pathname)) {
          publicMetaRequests.push({
            path: url.pathname,
            authorization: request.headers().authorization ?? "",
          });
        }
      });
      await win.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});
      await win.evaluate(() => {
        window.localStorage.setItem("echodesk.onboarding.completed", "1");
      });
      await win.reload({ waitUntil: "domcontentloaded" });
      await win.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});

      // 1. 品牌名出现 → 没白屏
      await expect(win.locator("text=EchoDesk").first()).toBeVisible({ timeout: 60_000 });

      // 2. 显式 public demo 不应要求本机 Python/backend。
      await expect
        .poll(
          async () =>
            win.evaluate(
              () =>
                (window as unknown as { echo?: { isPublicDemo?: boolean } })
                  .echo?.isPublicDemo === true,
            ),
          { timeout: 10_000 },
        )
        .toBe(true);
      await expect
        .poll(
          async () =>
            win.evaluate(async () => window.echo?.getBackendHost?.()),
          { timeout: 10_000 },
        )
        .toBe(PUBLIC_BACKEND_BASE);
      const publicMeta = await win.evaluate(async () => {
        const base = await window.echo?.getBackendHost?.();
        if (!base) throw new Error("public backend host unavailable");
        const [healthResponse, bootstrapResponse] = await Promise.all([
          fetch(`${base}/healthz`, { cache: "no-store", credentials: "omit" }),
          fetch(`${base}/bootstrap`, { cache: "no-store", credentials: "omit" }),
        ]);
        return {
          healthStatus: healthResponse.status,
          health: (await healthResponse.json()) as Record<string, unknown>,
          bootstrapStatus: bootstrapResponse.status,
          bootstrap: (await bootstrapResponse.json()) as Record<string, unknown>,
        };
      });
      expect(publicMeta.healthStatus).toBe(200);
      expect(publicMeta.health).toEqual({ status: "ok" });
      expect(publicMeta.bootstrapStatus).toBe(200);
      expect(publicMeta.bootstrap.session_required).toBe(true);
      expect(publicMeta.bootstrap.minimum_client_version).toBe(CLIENT_VERSION);
      expect(publicMeta.bootstrap.ws_path).toBe("/ws/echo");
      expect(publicMeta.bootstrap).not.toHaveProperty("backend_version");

      const authenticatedTransport = await win.evaluate(async (clientVersion) => {
        const base = await window.echo?.getBackendHost?.();
        const session = await window.echo?.ensurePublicSession?.();
        if (!base || !session?.token) {
          throw new Error("server-issued public session unavailable");
        }
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
            socket.close(1000, "packaged public E2E complete");
            resolve(message.type);
          };
          socket.onerror = () => {
            window.clearTimeout(timer);
            reject(new Error("public websocket handshake failed"));
          };
        });
        return {
          meetingsStatus: meetings.status,
          wsType,
          sessionIssued: Boolean(session.token),
        };
      }, CLIENT_VERSION);
      expect(authenticatedTransport).toEqual({
        meetingsStatus: 200,
        wsType: "server_hello",
        sessionIssued: true,
      });

      // 3. WS 已握手。当前 UI 把旧的连接 pill 合并进顶部 StatusBar，
      //    `.app-connection-status` 会被视觉隐藏，但仍保留文本状态供 E2E 断言。
      await expect(win.locator(".app-connection-status")).toHaveText("已连接", {
        timeout: 60_000,
      });
      await expect(win.getByTestId("pill-backend")).toBeVisible({ timeout: 15_000 });
      await expect
        .poll(() => publicMetaRequests.some((request) => request.path === "/healthz"))
        .toBe(true);
      expect(
        publicMetaRequests.filter((request) => request.path === "/healthz"),
      ).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ authorization: "" }),
        ]),
      );
      expect(publicMetaRequests.some((request) => request.path === "/healthz/full")).toBe(
        false,
      );
      await win.getByTestId("pill-backend").click();
      const backendPopover = win.locator(".ant-popover").filter({ hasText: "服务端" }).last();
      await expect(backendPopover).toContainText(/正常运行|已连接/, { timeout: 15_000 });
      await expect(backendPopover.getByText("版本", { exact: true })).toHaveCount(0);
      await win.keyboard.press("Escape");

      // 4. outputs 面板
      await expect(win.locator("text=outputs").first()).toBeVisible();
      await expect(win.locator("text=/^产物$/")).toHaveCount(0);
      await expect(win.getByRole("button", { name: /^生成$/ })).toHaveCount(0);

      // 5. 设置 / 工作区引导 / 输入框核心路径可点击。
      await win.getByTestId("open-settings").click();
      await expect(win.getByText("移动端连接")).toBeVisible({ timeout: 15_000 });
      await expect(win.locator("body")).not.toContainText("激活码");
      await win.keyboard.press("Escape");

      await expect(win.getByTestId("workspace-config-btn")).toBeVisible({ timeout: 15_000 });
      await win.getByTestId("workspace-dirs-tag").click();
      await expect(win.getByText("管理知识库")).toBeVisible({ timeout: 15_000 });
      await win.locator(".ant-modal .ant-modal-close").click();
      await expect(win.getByText("管理知识库")).toBeHidden({ timeout: 10_000 });

      const ta = win.locator("textarea[placeholder*='生成']");
      await ta.fill("跨平台打包测试");
      await expect(ta).toHaveValue("跨平台打包测试");

      // 6. CaptureStatus 文案
      const cap = win.getByTestId("capture-status");
      await expect(cap).toBeVisible({ timeout: 15_000 });
      await expect(cap).not.toContainText("@开始会议");

      // 7. UI 一致性：核心区域必须在视口内，且不能出现横向溢出。
      const layout = await win.evaluate(() => {
        const pick = (selector: string) => {
          const el = document.querySelector<HTMLElement>(selector);
          if (!el) return null;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          const placeholderStyle = window.getComputedStyle(el, "::placeholder");
          return {
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            right: rect.right,
            bottom: rect.bottom,
            fontSize: style.fontSize,
            placeholderFontSize: placeholderStyle.fontSize,
            display: style.display,
            visibility: style.visibility,
          };
        };
        const pickAny = (selectors: string[]) => {
          for (const selector of selectors) {
            const rect = pick(selector);
            if (rect) return rect;
          }
          return null;
        };

        return {
          viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
          },
          body: {
            scrollWidth: document.documentElement.scrollWidth,
            scrollHeight: document.documentElement.scrollHeight,
          },
          workspace: pick("[data-testid='workspace-bar']"),
          transcript: pickAny([
            "[data-testid='transcript-scroller']",
            ".echodesk-transcript-empty",
          ]),
          conversationTitle: pick("[data-testid='conversation-mode-title']"),
          transcriptA11y: (() => {
            const el = document.querySelector<HTMLElement>(
              "[data-testid='transcript-title']",
            );
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return {
              text: el.textContent?.trim() ?? "",
              width: rect.width,
              height: rect.height,
              position: style.position,
              overflow: style.overflow,
            };
          })(),
          command: pick("textarea[placeholder*='生成']"),
          capture: pick("[data-testid='capture-status']"),
          settingsButton: pick("[data-testid='open-settings']"),
          aiEnginePill: pick("[data-testid='pill-ai-engine']"),
        };
      });

      expect(layout.body.scrollWidth).toBeLessThanOrEqual(layout.viewport.width + 2);
      const workspace = requireRect(layout.workspace, "workspace bar");
      const transcript = requireRect(layout.transcript, "transcript scroller");
      const conversationTitle = requireRect(layout.conversationTitle, "conversation title");
      const command = requireRect(layout.command, "command textarea");
      const captureStatus = requireRect(layout.capture, "capture status");
      const settingsButton = requireRect(layout.settingsButton, "settings button");
      const aiEnginePill = requireRect(layout.aiEnginePill, "AI engine pill");

      for (const [name, rect] of Object.entries({
        workspace,
        transcript,
        conversationTitle,
        command,
        captureStatus,
        settingsButton,
        aiEnginePill,
      })) {
        expect(rect.display, `${name} display`).not.toBe("none");
        expect(rect.visibility, `${name} visibility`).not.toBe("hidden");
        expect(rect.width, `${name} width`).toBeGreaterThan(24);
        expect(rect.height, `${name} height`).toBeGreaterThanOrEqual(20);
        expect(rect.x, `${name} left bound`).toBeGreaterThanOrEqual(-1);
        expect(rect.right, `${name} right bound`).toBeLessThanOrEqual(layout.viewport.width + 2);
        expect(rect.bottom, `${name} bottom bound`).toBeLessThanOrEqual(
          layout.viewport.height + 2,
        );
      }

      expect(transcript.height).toBeGreaterThan(200);
      expect(conversationTitle.height).toBeGreaterThan(10);
      expect(conversationTitle.height).toBeLessThanOrEqual(36);
      expect(conversationTitle.x).toBeGreaterThanOrEqual(-1);
      expect(conversationTitle.right).toBeLessThanOrEqual(layout.viewport.width + 2);
      expect(Number.parseFloat(conversationTitle.fontSize)).toBeGreaterThanOrEqual(12);
      expect(layout.transcriptA11y).toEqual({
        text: "对话流",
        width: 1,
        height: 1,
        position: "absolute",
        overflow: "hidden",
      });
      expect(command.height).toBeGreaterThanOrEqual(44);
      const commandFontSize = Number.parseFloat(command.fontSize);
      expect(commandFontSize).toBeGreaterThanOrEqual(14);
      expect(commandFontSize).toBeLessThanOrEqual(15);
      expect(command.placeholderFontSize).toBe(command.fontSize);
      expect(settingsButton.height).toBeGreaterThanOrEqual(32);
      expect(settingsButton.width).toBeGreaterThanOrEqual(32);
      expect(aiEnginePill.height).toBeGreaterThanOrEqual(32);

      // 8. 截图存证
      await win.screenshot({
        path: "test-results/electron-app-final.png",
        fullPage: true,
      });
    } finally {
      await app.close();
    }
  });
});

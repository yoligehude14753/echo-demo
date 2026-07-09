/**
 * Electron 打包 App E2E（真发布版二进制）。
 *
 * 直接 launch 打包后的平台二进制：
 *   - 默认 macOS: /Applications/EchoDesk.app/Contents/MacOS/EchoDesk
 *   - Windows/Linux: 通过 ECHODESK_APP_BIN 指定 exe/AppImage/解包二进制
 * 让 Electron 自己 spawn 后端，验证：
 *   1. 主窗口启动不白屏（首个 BrowserWindow 加载完毕）
 *   2. public demo 桌面包默认走云端 backend，不要求本机 Python
 *   3. WS 与 backend 真握手成功（"已连接"）
 *   4. Settings / Workspace / CommandBar 核心点击路径可用
 *
 * 注意：
 * - 这是"打包后真二进制"路径，覆盖 vite dev 之外的 Electron 主进程 + preload + release 资源路径
 * - 未设置 ECHODESK_APP_BIN 时，非 macOS 会跳过
 */
import { test, expect, _electron as electron, type Page } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

function defaultAppBin(): string | null {
  if (process.env.ECHODESK_APP_BIN) return process.env.ECHODESK_APP_BIN;
  if (process.platform === "darwin") {
    return "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk";
  }
  return null;
}

const APP_BIN = defaultAppBin();

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

  test("packaged app: 启动不白屏 + public backend + 核心交互可点击", async () => {
    test.setTimeout(180_000);
    const env = {
      ...process.env,
      ECHO_PUBLIC_BACKEND_BASE:
        process.env.ECHO_PUBLIC_BACKEND_BASE ?? "https://echodesk.yoliyoli.uk",
    };
    delete env.ECHO_PUBLIC_DEMO;
    delete env.ECHO_FORCE_LOCAL_BACKEND;
    delete env.ECHO_BACKEND_PORT;

    const app = await electron.launch({
      executablePath: APP_BIN!,
      cwd: path.dirname(APP_BIN!),
      env,
      timeout: 60_000,
    });

    try {
      const appVersion = await app.evaluate(async ({ app }) => app.getVersion());
      // 第一个窗口 = 主 BrowserWindow
      const win: Page = await app.firstWindow({ timeout: 60_000 });
      // 等 React 渲染
      await win.waitForLoadState("domcontentloaded");
      await win.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});
      await win.evaluate(() => {
        window.localStorage.setItem("echodesk.onboarding.completed", "1");
      });
      await win.reload({ waitUntil: "domcontentloaded" });
      await win.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});

      // 1. 品牌名出现 → 没白屏
      await expect(win.locator("text=EchoDesk").first()).toBeVisible({ timeout: 60_000 });

      // 2. release 桌面包默认是 public demo，不应要求本机 Python/backend。
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
        .toBe("https://echodesk.yoliyoli.uk");
      const backendHealth = await win.evaluate(async () => {
        const base = await window.echo?.getBackendHost?.();
        const resp = await fetch(`${base}/healthz/full`);
        return (await resp.json()) as { backend?: { version?: string } };
      });
      expect(backendHealth.backend?.version).toBe(appVersion);

      // 3. WS 已握手。当前 UI 把旧的连接 pill 合并进顶部 StatusBar，
      //    `.app-connection-status` 会被视觉隐藏，但仍保留文本状态供 E2E 断言。
      await expect(win.locator(".app-connection-status")).toHaveText("已连接", {
        timeout: 60_000,
      });
      await expect(win.getByTestId("pill-backend")).toBeVisible({ timeout: 15_000 });
      await win.getByTestId("pill-backend").click();
      const backendPopover = win.locator(".ant-popover").filter({ hasText: "服务端" }).last();
      await expect(backendPopover.getByText(appVersion, { exact: true })).toBeVisible({
        timeout: 15_000,
      });
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
      await expect(win.getByText("知识库 / 工作区文件")).toBeVisible({ timeout: 15_000 });
      await win.locator(".ant-modal .ant-modal-close").click();
      await expect(win.getByText("知识库 / 工作区文件")).toBeHidden({ timeout: 10_000 });

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
          transcriptTitle: pick("[data-testid='transcript-title']"),
          command: pick("textarea[placeholder*='生成']"),
          capture: pick("[data-testid='capture-status']"),
          settingsButton: pick("[data-testid='open-settings']"),
          aiEnginePill: pick("[data-testid='pill-ai-engine']"),
        };
      });

      expect(layout.body.scrollWidth).toBeLessThanOrEqual(layout.viewport.width + 2);
      const workspace = requireRect(layout.workspace, "workspace bar");
      const transcript = requireRect(layout.transcript, "transcript scroller");
      const transcriptTitle = requireRect(layout.transcriptTitle, "transcript title");
      const command = requireRect(layout.command, "command textarea");
      const captureStatus = requireRect(layout.capture, "capture status");
      const settingsButton = requireRect(layout.settingsButton, "settings button");
      const aiEnginePill = requireRect(layout.aiEnginePill, "AI engine pill");

      for (const [name, rect] of Object.entries({
        workspace,
        transcript,
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
      expect(transcriptTitle.height).toBeGreaterThan(10);
      expect(transcriptTitle.height).toBeLessThanOrEqual(22);
      expect(transcriptTitle.x).toBeGreaterThanOrEqual(-1);
      expect(transcriptTitle.right).toBeLessThanOrEqual(layout.viewport.width + 2);
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

/**
 * Electron 打包 App E2E（真发布版二进制）。
 *
 * 直接 launch /Applications/EchoDesk.app/Contents/MacOS/EchoDesk，
 * 让 Electron 自己 spawn 后端，验证：
 *   1. 主窗口启动不白屏（首个 BrowserWindow 加载完毕）
 *   2. WS 与 spawn 出来的 backend 真握手成功（"已连接"）
 *   3. MeetingStatusBar 点击循环：idle → in_meeting → idle
 *   4. outputs 面板：标题 "outputs"、无 "生成" 按钮
 *   5. CommandBar 输入 @开始会议 不再触发 manual_start（已删 intent）
 *
 * 注意：
 * - 这是"打包后真二进制"路径，覆盖 vite dev 之外的 Electron 主进程 + 签名 + 后端 spawn
 * - 需要 /Applications/EchoDesk.app 存在；CI 跳过
 */
import { test, expect, _electron as electron, type Page } from "@playwright/test";
import { existsSync } from "node:fs";

const APP_BIN = "/Applications/EchoDesk.app/Contents/MacOS/EchoDesk";

test.describe("EchoDesk 打包 App", () => {
  test.skip(!existsSync(APP_BIN), "未安装 /Applications/EchoDesk.app，跳过");

  test("packaged app: 启动不白屏 + 核心交互可点击", async () => {
    test.setTimeout(180_000);

    // 让 Electron 跑 ad-hoc 签名后能起；不传 args
    const app = await electron.launch({
      executablePath: APP_BIN,
      // 给打包后的 backend 启动足够时间（首次 spawn 装载 SQLite/模型）
      timeout: 60_000,
    });

    try {
      // 第一个窗口 = 主 BrowserWindow
      const win: Page = await app.firstWindow({ timeout: 60_000 });
      // 等 React 渲染
      await win.waitForLoadState("domcontentloaded");
      await win.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});

      // 1. 品牌名出现 → 没白屏
      await expect(win.locator("text=EchoDesk").first()).toBeVisible({ timeout: 60_000 });

      // 2. WS 已握手（后端是 Electron spawn 的本地后端，可能稍慢）
      await expect(win.locator("text=已连接")).toBeVisible({ timeout: 60_000 });

      // 3. MeetingStatusBar 点击循环
      const bar = win.locator("[data-testid='meeting-status-bar']");
      await expect(bar).toBeVisible();
      // 如果 hydrate 出 in_meeting，先点回 idle
      if (((await bar.textContent()) ?? "").includes("会议中")) {
        await bar.click();
        await expect(bar).toContainText("待机", { timeout: 15_000 });
      }
      await expect(bar).toContainText("待机");
      await bar.click();
      await expect(bar).toContainText("会议中", { timeout: 15_000 });
      await bar.click();
      await expect(bar).toContainText("待机", { timeout: 20_000 });

      // 4. outputs 面板
      await expect(win.locator("text=outputs").first()).toBeVisible();
      await expect(win.locator("text=/^产物$/")).toHaveCount(0);
      await expect(win.getByRole("button", { name: /^生成$/ })).toHaveCount(0);

      // 5. CommandBar @开始会议 不应触发 manual_start
      const ta = win.locator("textarea[placeholder*='生成']");
      await ta.fill("@开始会议");
      await ta.press("Enter");
      await win.waitForTimeout(6_000);
      await expect(
        win.locator(".ant-message").filter({ hasText: /已开启/ }),
      ).toHaveCount(0);

      // 6. CaptureStatus 文案
      const cap = win.getByTestId("capture-status");
      await expect(cap).toBeVisible({ timeout: 15_000 });
      await expect(cap).not.toContainText("@开始会议");

      // 7. 截图存证
      await win.screenshot({
        path: "test-results/electron-app-final.png",
        fullPage: true,
      });
    } finally {
      await app.close();
    }
  });
});

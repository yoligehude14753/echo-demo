import { defineConfig, devices } from "@playwright/test";

const chromiumExecutablePath = process.env.ECHODESK_CHROMIUM_PATH || undefined;

/**
 * 场景验证 + 录像（P3 收官产物）
 *
 * 跟 playwright.config.ts 的区别：
 *  - 强制每个 case 录视频（webm 1280x800@25fps）
 *  - 失败时多保留 trace 与 screenshot
 *  - slowMo 100ms 让录像可观看（默认是瞬时点击）
 *  - 输出到 test-results/scenarios/ 一个独立目录，便于打包给用户
 *
 * 用法：
 *   npm run scenarios          # 跑全套场景并录像
 *   npm run scenarios -- --grep "P3.1"   # 只跑某个场景
 *
 * 视频在：desktop/test-results/scenarios/<test-name>/video.webm
 * 转 mp4：ffmpeg -i video.webm -c:v libx264 -crf 18 -pix_fmt yuv420p out.mp4
 */
export default defineConfig({
  testDir: "./tests/scenarios",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "test-results/scenarios-html", open: "never" }]],
  outputDir: "test-results/scenarios",
  timeout: 90_000,
  use: {
    baseURL: "http://localhost:5175",
    trace: "on",
    screenshot: "on",
    video: {
      mode: "on",
      size: { width: 1280, height: 800 },
    },
    viewport: { width: 1280, height: 800 },
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    launchOptions: {
      ...(chromiumExecutablePath ? { executablePath: chromiumExecutablePath } : {}),
      slowMo: 120, // 每个操作之间留 120ms，让视频可读
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    command: "npm run dev -- --port 5175 --host 127.0.0.1 --strictPort",
    url: "http://localhost:5175",
    timeout: 60_000,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
  },
});

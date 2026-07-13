import { defineConfig, devices } from "@playwright/test";

/**
 * Demo 录屏配置：连接真后端、开启视频录制、慢节奏演示用。
 *
 * 用法：
 *   1. 启动 backend (Yunwu key 已配，可选 WORKSPACE_DIRS=...)
 *      cd backend && uvicorn app.main:app --port 8769 --ws-max-size 4096
 *   2. 启动 dev server
 *      VITE_API_TARGET=http://localhost:8769 npm run dev -- --port 5173
 *   3. npm run demo:record
 *   4. 视频产物在 test-results/demo-recording/*.webm
 *      用 ffmpeg 转 mp4：ffmpeg -i input.webm -c:v libx264 -crf 18 out.mp4
 *
 * 视频规格：1280x800（默认 Desktop Chrome），帧率 25fps。
 */
export default defineConfig({
  testDir: "./tests/e2e-real",
  testMatch: /demo-recording\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  outputDir: "test-results/demo-recording",
  use: {
    baseURL: "http://localhost:5173",
    trace: "off",
    screenshot: "on",
    video: {
      mode: "on",
      size: { width: 1280, height: 800 },
    },
    viewport: { width: 1280, height: 800 },
    actionTimeout: 60_000,
    navigationTimeout: 30_000,
  },
  timeout: 600_000, // 10 min cap
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
});

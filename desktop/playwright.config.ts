import { defineConfig, devices } from "@playwright/test";

const chromiumExecutablePath = process.env.ECHODESK_CHROMIUM_PATH || undefined;

/**
 * Echo desktop E2E（PR-15 / m5-t4）。
 *
 * 设计：
 * - 用 Vite 起 dev server，所有 backend API 与 WS 走 Playwright route mock
 * - 不依赖真实 backend / LLM，CI 可跑
 * - 真实集成验证由 backend integration tests 与本地 demo_run_quick.py 负责
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "list" : "list",
  use: {
    baseURL: "https://localhost:5174",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 8_000,
    navigationTimeout: 15_000,
    launchOptions: chromiumExecutablePath
      ? { executablePath: chromiumExecutablePath }
      : undefined,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev:e2e:https",
    url: "https://localhost:5174",
    ignoreHTTPSErrors: true,
    timeout: 60_000,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
  },
});

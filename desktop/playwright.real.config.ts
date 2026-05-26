import { defineConfig, devices } from "@playwright/test";

/**
 * 真后端 E2E：连真 backend (:8766) + 真 LLM (Yunwu M2.7) + 真 RAG。
 *
 * 用法：
 *   1. 启动 backend: uvicorn app.main:app --port 8766
 *   2. 启动 dev server: VITE_API_TARGET=http://localhost:8766 npm run dev -- --port 5173
 *   3. npm run e2e:real
 *
 * 不在 CI 跑（需要 YUNWU_OPEN_KEY + heyi-bj 在线）。
 */
export default defineConfig({
  testDir: "./tests/e2e-real",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 60_000,
    navigationTimeout: 30_000,
  },
  timeout: 240_000,
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // 真后端 E2E 假设 dev server 已在外部启动，不自动拉起
});

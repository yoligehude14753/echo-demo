/**
 * E2E #2：产物生成新流程（2026-05 改版后）
 *
 * - 旧版："生成"按钮 → modal → 填 brief → 确认。ArtifactPanel 删按钮后失效。
 * - 新版：CommandBar 输入 @生成 HTML XX → extractExplicitArtifactCommand 命中
 *         → 直连 /artifacts/generate/stream（确定性 skill 流，不绕 agent）
 *         → done 事件落 store → ArtifactPanel 展示卡片
 *
 * 关键：mock /intent/route 直接返回 generate_html，避免依赖远端 LLM 分类。
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("@生成 命令触发产物生成流程，卡片出现", async ({ page }) => {
  // 拦截 /intent/route：测试不该依赖远端 LLM 分类
  await page.route(/\/intent\/route$/, async (route) => {
    const req = route.request();
    const body = req.postDataJSON() ?? {};
    const text = (body.text as string) ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_html",
        confidence: 0.95,
        params: {
          artifact_type: "html",
          brief: text.replace(/^@\S+\s*/, "") || "测试 HTML 报告",
        },
      }),
    });
  });

  const mock = await installEchoMock(page, { skipPaths: ["/intent/route"] });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 1. 在 CommandBar 输入 @生成 HTML
  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@生成 HTML 测试报告");

  // 2. 按 Enter 提交（CommandBar 的快捷键）
  await textarea.press("Enter");

  // 3. 显式 @生成 走直连 skill 流（/artifacts/generate/stream），不经 agent
  await expect
    .poll(
      async () => {
        const log = await mock.fetchLog();
        return log.find(
          (r) => r.method === "POST" && r.url.includes("/artifacts/generate/stream"),
        );
      },
      { timeout: 5_000 },
    )
    .toBeTruthy();

  // 4. ArtifactPanel 应该能看到 agent SSE 返回的 artifact（M4 改版后 title 主显示）
  await expect(
    page.getByTestId("artifact-card").filter({ hasText: "mock html 报告" }),
  ).toBeVisible({ timeout: 5_000 });
});

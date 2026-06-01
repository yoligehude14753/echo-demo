/**
 * 场景 4：会议主流程 — @生成 命令触发产物
 *
 * 覆盖功能：
 *  - 命令栏 @生成 HTML 输入 + Enter 提交
 *  - intent 路由分类（mock generate_html）
 *  - /agent/run POST 调用
 *  - agent SSE artifact 事件 → ArtifactPanel 卡片展示
 *
 * 视频里观察点：
 *  - 用户在底部 CommandBar 输入文本
 *  - 按 Enter 后命令气泡消失，右侧 ArtifactPanel 出现卡片
 *  - WebSocket 状态 pill 全程绿色
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

test("S04 · @生成 HTML 命令 → 产物卡片出现", async ({ page }) => {
  // intent/route mock：把任意 @生成 HTML 文本归类为 generate_html
  await page.route(/\/intent\/route$/, async (route) => {
    const body = route.request().postDataJSON() ?? {};
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_html",
        confidence: 0.95,
        params: {
          artifact_type: "html",
          brief: ((body.text as string) ?? "").replace(/^@\S+\s*/, "") || "测试 HTML 报告",
        },
      }),
    });
  });

  const mock = await installScenarioMock(page, { skipPaths: ["/intent/route"] });

  await test.step("打开主界面，等连接 OK", async () => {
    await page.goto("/");
    await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });
  });

  await test.step("在 CommandBar 输入 @生成 HTML 测试报告", async () => {
    const textarea = page.getByTestId("command-textarea");
    await textarea.click();
    await textarea.fill("@生成 HTML 测试报告");
    await expect(textarea).toHaveValue("@生成 HTML 测试报告");
  });

  await test.step("按 Enter 提交，命令被发送到 /agent/run", async () => {
    await page.getByTestId("command-textarea").press("Enter");
    await expect
      .poll(
        async () => {
          const log = await mock.fetchLog();
          return log.find(
            (r) => r.method === "POST" && r.url.includes("/agent/run"),
          );
        },
        { timeout: 5_000 },
      )
      .toBeTruthy();
  });

  await test.step("agent SSE 返回 artifact → 卡片出现", async () => {
    await expect(
      page.getByTestId("artifact-card").filter({ hasText: "mock html 报告" }),
    ).toBeVisible({ timeout: 5_000 });
  });

  await test.step("textarea 已清空，可继续输入下一条命令", async () => {
    await expect(page.getByTestId("command-textarea")).toHaveValue("");
  });
});

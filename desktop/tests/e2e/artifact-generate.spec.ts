/**
 * E2E #2：产物生成 modal → fetch /artifacts/generate → ws.artifact.ready → 卡片出现。
 */
import { test, expect } from "@playwright/test";
import { installEchoMock, publishArtifactReady } from "./_mock";

test("点击生成按钮触发产物生成流程，卡片出现", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 1. 打开 modal
  await page.getByRole("button", { name: "生成" }).first().click();
  await expect(page.locator("text=生成产物")).toBeVisible();

  // 2. 选 HTML（默认即是）+ 填 brief
  // 注：页面里另有 CommandBar 的 textarea，必须限定在 modal 里
  const brief = "生成一份测试 HTML 报告";
  await page.locator(".ant-modal textarea:not([aria-hidden=\"true\"])").fill(brief);

  // 3. 点击确认
  await page.locator(".ant-modal-footer button.ant-btn-primary").click();

  // 4. fetch log 应该有 /artifacts/generate POST
  await expect.poll(async () => {
    const log = await mock.fetchLog();
    return log.find((r) => r.method === "POST" && r.url.includes("/artifacts/generate"));
  }, { timeout: 5_000 }).toBeTruthy();

  // 5. 推 artifact.ready 事件
  const artifactId = await publishArtifactReady(mock, "html", 1, "mock-html-e2e-001");

  // 6. ArtifactPanel 列表里能看到这个 artifact
  await expect(page.locator(`text=${artifactId}`)).toBeVisible({ timeout: 5_000 });
});

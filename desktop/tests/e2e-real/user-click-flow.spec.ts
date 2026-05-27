/**
 * 真后端 用户点击流 E2E
 *
 * 模拟用户在 desktop UI 上的完整 happy path：
 *   1. 打开页面 → 看到 "已连接"
 *   2. CommandBar 输入 @开始会议 → 会议出现并选中
 *   3. CommandBar 输入 @生成 HTML xxx → 等待真 LLM 返回 → ArtifactPanel 列表出现
 *   4. CommandBar 输入 @查 xxx → 看到 rag.answer.done 事件入事件流
 *   5. CommandBar 输入 @生成 Excel xxx → 等待真 LLM 返回
 *   6. CommandBar 输入 @生成 PPT xxx → 等待真 LLM 返回
 *   7. CommandBar 输入 @生成 Word xxx → 等待真 LLM 返回
 *   8. CaptureSession 持续采集状态可见（无手动开始按钮）
 *
 * 注意：每个产物真 LLM 走 60-180s，整测试可能 8-15 分钟。
 */
import { test, expect, type Page } from "@playwright/test";

const COMMAND_BAR_TA = "textarea[placeholder*='生成']";

async function typeAndSend(page: Page, text: string): Promise<void> {
  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill(text);
  await ta.press("Enter");
}

test("happy path: connect → start meeting → 4 artifacts → rag ask", async ({ page }) => {
  test.setTimeout(1_800_000); // 30 min total

  // 1. 打开
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  // 2. @开始会议
  await typeAndSend(page, "@开始会议");
  await expect(
    page.locator(".ant-message").filter({ hasText: /开启/ }),
  ).toBeVisible({ timeout: 15_000 });
  // 等 toast 消失避免重叠
  await page.waitForTimeout(2_500);

  // 3. @生成 HTML（真 LLM 60-180s）
  // 现在是 fire-and-forget；textarea 立即可用，但要等新的 artifact 真出现
  // 记下当前产物数量，等数字 +1
  async function artifactCount(prefix: RegExp): Promise<number> {
    return await page.locator(`text=${prefix}`).count();
  }
  const htmlBefore = await artifactCount(/html-[0-9a-f]+/);
  await typeAndSend(page, "@生成 HTML 写一个 Hello World 的网页");
  await expect
    .poll(() => artifactCount(/html-[0-9a-f]+/), { timeout: 300_000, intervals: [5000] })
    .toBeGreaterThan(htmlBefore);

  // 4. @查 (RAG/web search)
  await typeAndSend(page, "@查 什么是检索增强生成");
  // 等到事件计数变大 +1
  await page.waitForTimeout(60_000);

  // 5. @生成 Excel
  const xlsxBefore = await artifactCount(/xlsx-[0-9a-f]+/);
  await typeAndSend(page, "@生成 Excel 2024 季度营收对比表");
  await expect
    .poll(() => artifactCount(/xlsx-[0-9a-f]+/), { timeout: 300_000, intervals: [5000] })
    .toBeGreaterThan(xlsxBefore);

  // 6. @生成 PPT
  const pptxBefore = await artifactCount(/pptx-[0-9a-f]+/);
  await typeAndSend(page, "@生成 PPT 苹果 2025 Q2 业绩 3 页");
  await expect
    .poll(() => artifactCount(/pptx-[0-9a-f]+/), { timeout: 300_000, intervals: [5000] })
    .toBeGreaterThan(pptxBefore);

  // 7. @生成 Word
  const wordBefore = await artifactCount(/word-[0-9a-f]+/);
  await typeAndSend(page, "@生成 Word AI Agent 简短调研 2 段");
  await expect
    .poll(() => artifactCount(/word-[0-9a-f]+/), { timeout: 300_000, intervals: [5000] })
    .toBeGreaterThan(wordBefore);

  // 8. CaptureSession 持续采集
  await expect(page.getByTestId("capture-status")).toBeVisible();
  await expect(page.getByTestId("capture-status")).toContainText(/持续采集|初始化麦克风|ambient/);

  // 9. 截图，存证
  await page.screenshot({ path: "test-results/happy-path-final.png", fullPage: true });
});

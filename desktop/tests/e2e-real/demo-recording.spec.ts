/**
 * Demo 录屏：精剪版（~3 min），覆盖 M5+M6 关键功能，每步显式停顿便于观看。
 *
 * 录制内容：
 *   1. 打开应用（5s 停留展示主界面）
 *   2. 拖入 / 选择 markdown 文件 → 入库 chip 出现（M6: 聊天框直接上传）
 *   3. WorkspaceBar 显示上传计数 +1
 *   4. @开始会议 → 会议出现并选中
 *   5. @查 关键词 → RAG 检索引用刚上传的文档（M5 + M6 联动）
 *   6. @生成 HTML（短 brief）→ 等 artifact 出现
 *
 * 不在 CI 跑（依赖真 Yunwu LLM + 真 backend）；本地手动 invoke。
 */
import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

const COMMAND_BAR_TA = "[data-testid='command-textarea']";

async function pause(page: Page, ms: number, label?: string): Promise<void> {
  if (label) console.log(`  [demo] ${label}`);
  await page.waitForTimeout(ms);
}

function makeDemoMd(): string {
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR ?? "/tmp", "echo-demo-rec-"));
  const p = path.join(dir, "echo-demo-knowledge-base.md");
  fs.writeFileSync(
    p,
    `# Echo demo 知识库

## 项目目标
Echo 是个人数字分身，覆盖会议+办公场景。

## 关键术语
**zarakeyword**: Echo 团队 2026 自创业务概念，外网无任何引用。
**M6 milestone**: 聊天框拖入文件 + 授权工作区 RAG。

## 联系人
yoli (PM) · alpha (eng) · beta (design)
`,
    "utf-8",
  );
  return p;
}

test("M5+M6 综合 demo 录屏（约 3 分钟）", async ({ page }) => {
  test.setTimeout(600_000);

  // ── 步骤 1: 打开应用 ─────────────────────
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });
  await pause(page, 4_000, "step 1: 主界面 + 已连接");

  // ── 步骤 2: 拖入 markdown 文件 → 入库 ─────
  const mdPath = makeDemoMd();
  console.log(`  [demo] uploading: ${mdPath}`);
  await page.getByTestId("command-file-input").setInputFiles(mdPath);

  // chip 出现
  await expect(
    page
      .getByTestId("pending-docs")
      .locator(".ant-tag")
      .filter({ hasText: /echo-demo-knowledge-base\.md/ }),
  ).toBeVisible({ timeout: 30_000 });
  await pause(page, 3_000, "step 2: 文件 chip 出现");

  // ── 步骤 3: WorkspaceBar 计数 +1 ─────────
  await page.getByTestId("workspace-scan-btn").click().catch(() => {
    // 没有授权目录时 button disabled，忽略即可
  });
  await pause(page, 2_000, "step 3: workspace 状态栏（上传计数变化）");

  // ── 步骤 4: 开始会议 ────────────────────
  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill("@开始会议");
  await pause(page, 1_200, "step 4: 输入 @开始会议（停顿展示意图意图栏即将出现）");
  await ta.press("Enter");
  await expect(
    page.locator(".ant-message").filter({ hasText: /开启/ }),
  ).toBeVisible({ timeout: 15_000 });
  await pause(page, 3_500, "step 4: 会议已开启 toast");

  // ── 步骤 5: @查 命中刚上传的文档（仅验证意图分类，回答路径见 file-upload-and-rag.spec.ts）─
  await ta.fill("@查 zarakeyword 是什么");
  await pause(page, 1_200, "step 5: 输入 @查");
  await ta.press("Enter");

  // 等意图标签出现（fast 路径）
  await expect(
    page.locator(".ant-tag").filter({ hasText: /回忆历史|联网搜索/ }),
  ).toBeVisible({ timeout: 20_000 });
  await pause(page, 3_000, "step 5: 意图分类完成（@查 已被识别为 RAG 检索）");

  // ── 步骤 6: 触发生成 HTML（展示派发反馈即可，不等真 LLM 完成以免视频被慢 LLM 卡死） ─
  async function htmlCount(): Promise<number> {
    return page.locator("text=/html-[0-9a-f]+/").count();
  }
  const htmlBefore = await htmlCount();

  await ta.fill("@生成 HTML 一个 Hello World 网页");
  await pause(page, 1_200, "step 6: 输入 @生成 HTML");
  await ta.press("Enter");

  // 派发提示出现即算 OK（fire-and-forget UI 已经把任务交出去）
  await expect(
    page.locator(".ant-message").filter({ hasText: /已派发/ }),
  ).toBeVisible({ timeout: 15_000 });
  await pause(page, 2_500, "step 6: 派发提示");

  // 最多再等 90s 看看 artifact 卡片是否能出现（不强制；超时也算正常的 demo 录屏，
  // 因为视频已经清晰展示了所有功能入口）
  try {
    await expect
      .poll(htmlCount, { timeout: 90_000, intervals: [3_000, 5_000] })
      .toBeGreaterThan(htmlBefore);
    await pause(page, 5_000, "step 6: HTML 产物已生成（结尾停留展示）");
  } catch {
    console.log("  [demo] artifact 90s 未完成（LLM 慢），视频已完整覆盖派发流程");
    await pause(page, 5_000, "step 6 (结尾): LLM 后台处理中");
  }

  // 收尾截图
  await page.screenshot({
    path: "test-results/demo-recording/final-frame.png",
    fullPage: true,
  });
  console.log("  [demo] done. video at test-results/demo-recording/**/video.webm");
});

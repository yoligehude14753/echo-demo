/**
 * 场景 7（P4-cmdbar-fixes，2026-05-28）：命令栏 Send 修复回归
 *
 * 复现用户截图的两个问题：
 *
 *  A. 「对话无法发送」
 *     用户截图：附件标签 "褐蚁使用手册.pdf X" 已附 + 输入框空 + 按 Enter。
 *     旧行为：onSubmit() 见 text.trim()==='' 静默 return → 用户看不到任何反馈
 *            （连 Send 按钮都没有，只能按 Enter）。
 *     新行为：
 *       - 多出一个 Send 按钮（data-testid="command-send-btn"）
 *       - 文本空 + 有附件时 Send 仍可点；点击后用默认 brief 走 /intent/route
 *         并在派发完成后清空 pendingDocs
 *
 *  C. 「置信度 100% 怎么得出的？」
 *     旧行为：无 @ 前缀输入 → 后端硬编码 confidence=1.0
 *            → 前端显示 "置信度 100%"（虚假置信感）
 *     新行为：后端返回 confidence=null（语义：本路径未跑分类器）
 *            → 前端显示 "规则匹配"
 *
 * 这两条 case 在本 commit 之前会失败；本 commit 之后才通过——这是回归测试的本意。
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock, publishArtifactReady } from "./_helpers";

test("S07a · 附件已附 + 文本空 → Send 按钮可点 → /intent/route 被调", async ({
  page,
}) => {
  // 拦截 /rag/ingest 用稳定 doc_id，方便 assert
  await page.route(/\/(api\/)?rag\/ingest$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        doc_id: "doc-pdf-s07-001",
        title: "褐蚁使用手册",
      }),
    });
  });

  // intent 路由：mock 成 chat 兜底（附件 + 默认 brief → "请基于附件回答"）
  let lastIntentBody: string | undefined;
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    const req = route.request();
    lastIntentBody = req.postData() ?? undefined;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "chat",
        confidence: null,
        params: {},
        rationale: "无 @ 前缀（规则匹配）",
      }),
    });
  });

  const mock = await installScenarioMock(page);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await test.step("用文件输入框模拟选中一个 PDF，等待入库完成", async () => {
    // 用 setInputFiles 注入虚拟 PDF；scenario mock 的 /rag/ingest 立即返
    const input = page.getByTestId("command-file-input");
    await input.setInputFiles({
      name: "褐蚁使用手册.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4 fake bytes\n"),
    });
    // 等附件 chip 出现，说明入库完成、uploading 归零
    await expect(
      page.locator('[data-testid="pending-docs"]'),
    ).toContainText("褐蚁使用手册.pdf", { timeout: 5_000 });
  });

  await test.step("textarea 留空 → Send 按钮 NOT disabled", async () => {
    const textarea = page.getByTestId("command-textarea");
    await expect(textarea).toHaveValue("");
    const sendBtn = page.getByTestId("command-send-btn");
    await expect(sendBtn).toBeEnabled();
  });

  await test.step("点击 Send → /intent/route 真的被调用（带默认 brief）", async () => {
    await page.getByTestId("command-send-btn").click();

    await expect
      .poll(
        async () => {
          const log = await mock.fetchLog();
          return log.find(
            (r) => r.method === "POST" && r.url.includes("/intent/route"),
          );
        },
        { timeout: 5_000 },
      )
      .toBeTruthy();

    expect(lastIntentBody, "intent/route body 应包含默认 brief").toBeDefined();
    expect(lastIntentBody).toContain("附件");
    expect(lastIntentBody).toContain("褐蚁使用手册.pdf");
  });

  await test.step("发送完毕：附件 chip 被清空（pendingDocs.length=0）", async () => {
    // pending-docs 容器只在 length>0 或 uploading>0 时渲染，所以这里期望它消失
    await expect(page.locator('[data-testid="pending-docs"]')).toHaveCount(0, {
      timeout: 5_000,
    });
  });
});

test("S07b · 无 @ 前缀输入 → 状态行显示「规则匹配」而不是「置信度 100%」", async ({
  page,
}) => {
  // 真实模拟后端「无 @ 前缀」分支：confidence=null
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "chat",
        confidence: null,
        params: { text: "今天天气怎么样" },
        rationale: "无 @ 前缀（规则匹配）",
      }),
    });
  });

  await installScenarioMock(page);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("今天天气怎么样");
  await textarea.press("Enter");

  // 关键断言：状态行出现「规则匹配」徽标
  await expect(page.getByTestId("intent-rule-matched")).toBeVisible({
    timeout: 5_000,
  });
  await expect(page.getByTestId("intent-rule-matched")).toHaveText("规则匹配");

  // 不应再有 "置信度 100%" 这种虚假数字
  await expect(page.getByTestId("intent-confidence")).toHaveCount(0);
  await expect(page.locator('[data-testid="intent-status"]')).not.toContainText(
    "置信度",
  );
  await expect(page.locator('[data-testid="intent-status"]')).not.toContainText(
    "100%",
  );
});

test("S07c · 命中关键字（@生成 PPT）→ 状态行仍显示真实「置信度 85%」", async ({
  page,
}) => {
  // 对比 case：分类器路径 confidence 是有意义的 float，必须保留百分比显示
  let receivedRequest = false;
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    receivedRequest = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_pptx",
        confidence: 0.85,
        params: { artifact_type: "pptx", brief: "测试 PPT" },
        rationale: "关键字命中",
      }),
    });
  });

  const mock = await installScenarioMock(page);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@生成 PPT 测试");
  await textarea.press("Enter");

  await expect
    .poll(() => receivedRequest, { timeout: 5_000 })
    .toBeTruthy();

  // 真实分类器路径：保留百分比
  await expect(page.getByTestId("intent-confidence")).toBeVisible({
    timeout: 5_000,
  });
  await expect(page.getByTestId("intent-confidence")).toContainText("85%");
  // 不应误判为「规则匹配」徽标（那是 confidence===null 的兜底）
  await expect(page.getByTestId("intent-rule-matched")).toHaveCount(0);

  // 同时 publish 一个 artifact.ready 让生成流程的副作用闭环（防止 vite teardown 报 noise）
  await publishArtifactReady(mock, "pptx", 1, "mock-pptx-s07c-001");
});

test("S07d · 文本+附件都空时 Send 按钮 disabled（不允许空发送）", async ({
  page,
}) => {
  await installScenarioMock(page);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const sendBtn = page.getByTestId("command-send-btn");
  await expect(sendBtn).toBeDisabled();
});

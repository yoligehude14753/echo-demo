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
 *  B. 普通文本走后端 intent route，再进入 RAG 回答闭环
 *  C. 显式 @生成 命令由本地确定性解析器直接派发，不依赖分类器置信度
 *
 * 这三条 case 共同保护命令栏的发送与派发闭环。
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

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
  await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });

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

test("S07b · 无 @ 前缀输入 → intent route → RAG 回答", async ({
  page,
}) => {
  let lastIntentBody: string | undefined;
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    lastIntentBody = route.request().postData() ?? undefined;
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
  await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("今天天气怎么样");
  await textarea.press("Enter");

  await expect.poll(() => lastIntentBody, { timeout: 5_000 }).toBeDefined();
  expect(lastIntentBody).toContain("今天天气怎么样");
  await expect(
    page.getByText("Echo 已收到，这是 TV 问答文本回复。", { exact: true }),
  ).toBeVisible({ timeout: 5_000 });
  await expect(page.getByTestId("transcript-scroller")).not.toContainText("置信度 100%");
});

test("S07c · 显式 @生成 PPT → 绕过分类器并派发 artifact", async ({
  page,
}) => {
  const mock = await installScenarioMock(page);

  await page.goto("/");
  await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@生成 PPT 测试");
  await textarea.press("Enter");

  await expect
    .poll(async () => {
      const log = await mock.fetchLog();
      return log.some(
        (entry) => entry.method === "POST" && entry.url.includes("/artifacts/generate"),
      );
    }, { timeout: 5_000 })
    .toBe(true);
  const request = (await mock.fetchLog()).find(
    (entry) => entry.method === "POST" && entry.url.includes("/artifacts/generate"),
  );

  expect(request?.bodyText).toBeDefined();
  const body = JSON.parse(request?.bodyText ?? "{}") as {
    artifact_type?: string;
    brief?: string;
  };
  expect(body.artifact_type).toBe("pptx");
  expect(body.brief).toContain("@生成 PPT 测试");
  expect((await mock.fetchLog()).some((entry) => entry.url.includes("/intent/route"))).toBe(
    false,
  );
  await expect(page.getByText("mock pptx 报告", { exact: true })).toBeVisible({
    timeout: 5_000,
  });
});

test("S07d · 文本+附件都空时 Send 按钮 disabled（不允许空发送）", async ({
  page,
}) => {
  await installScenarioMock(page);

  await page.goto("/");
  await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });

  const sendBtn = page.getByTestId("command-send-btn");
  await expect(sendBtn).toBeDisabled();
});

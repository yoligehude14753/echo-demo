import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

async function routeRagIntent(page: Page) {
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "search_rag",
        confidence: 1,
        params: { question: "测试 RAG 终帧" },
        rationale: "test",
      }),
    });
  });
}

async function submitQuestion(page: Page) {
  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("测试 RAG 终帧");
  await textarea.press("Enter");
}

test("RAG 只在 done 终帧后显示完整答案并播放 TTS", async ({ page }) => {
  await routeRagIntent(page);
  await page.route(/\/(api\/)?rag\/ask$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        "event: delta",
        'data: {"type":"delta","delta":"部分"}',
        "",
        "event: done",
        'data: {"type":"done","answer":"完整答案","sources":[{"kind":"rag","doc_id":"doc-1","title":"资料"}],"trace":{"chosen_source":"rag"},"meta":{"chosen_source":"rag","citations":[]}}',
        "",
        "",
      ].join("\n"),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/intent/route", "/rag/ask"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await submitQuestion(page);

  await expect(page.getByTestId("assistant-message").filter({ hasText: "完整答案" })).toBeVisible();
  await expect(page.locator(".ant-message-success").filter({ hasText: "已回答" })).toBeVisible();
  await expect.poll(async () => (await mock.fetchLog()).some((entry) => entry.url.includes("/tts/speak"))).toBe(true);
});

test("RAG error 终帧必须拒绝部分答案", async ({ page }) => {
  await routeRagIntent(page);
  await page.route(/\/(api\/)?rag\/ask$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        "event: delta",
        'data: {"type":"delta","delta":"不应保留的部分答案"}',
        "",
        "event: error",
        'data: {"type":"error","code":"answer_generation_failed","error":"暂时无法生成回答，请稍后重试"}',
        "",
        "",
      ].join("\n"),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/intent/route", "/rag/ask", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await submitQuestion(page);

  await expect(page.locator(".ant-message-error").filter({ hasText: "检索失败" })).toBeVisible();
  await expect(page.getByTestId("assistant-message").filter({ hasText: "不应保留" })).toHaveCount(0);
  await expect(page.locator(".ant-message-success").filter({ hasText: "已回答" })).toHaveCount(0);
  expect((await mock.fetchLog()).some((entry) => entry.url.includes("/tts/speak"))).toBe(false);
});

test("RAG 在 done 前 EOF 必须失败而不是假成功", async ({ page }) => {
  await routeRagIntent(page);
  await page.route(/\/(api\/)?rag\/ask$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: ['event: delta', 'data: {"type":"delta","delta":"截断答案"}', "", ""].join("\n"),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/intent/route", "/rag/ask", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await submitQuestion(page);

  await expect(page.locator(".ant-message-error").filter({ hasText: "检索失败" })).toBeVisible();
  await expect(page.getByTestId("assistant-message").filter({ hasText: "截断答案" })).toHaveCount(0);
  await expect(page.locator(".ant-message-success").filter({ hasText: "已回答" })).toHaveCount(0);
  expect((await mock.fetchLog()).some((entry) => entry.url.includes("/tts/speak"))).toBe(false);
});

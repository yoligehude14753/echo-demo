import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("Chat SSE 业务错误必须 reject，不得以部分答案成功收口", async ({ page }) => {
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "chat_no_rag",
        confidence: 1,
        params: { text: "测试 SSE 错误" },
        rationale: "test",
      }),
    });
  });
  await page.route(/\/(api\/)?chat$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        'data: {"delta":"不应保留的部分答案"}',
        "",
        "event: error",
        'data: {"error":"provider unavailable"}',
        "",
        "",
      ].join("\n"),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/intent/route", "/chat", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@chat 测试 SSE 错误");
  await textarea.press("Enter");

  await expect(
    page.locator(".ant-message-error").filter({ hasText: "暂时无法回复" }),
  ).toBeVisible({ timeout: 5_000 });
  await expect(
    page
      .getByTestId("assistant-message")
      .filter({ hasText: "不应保留的部分答案" }),
  ).toHaveCount(0);
  await expect(
    page.locator(".ant-message-success").filter({ hasText: "已回复" }),
  ).toHaveCount(0);

  const fetchLog = await mock.fetchLog();
  expect(fetchLog.some((entry) => entry.url.includes("/tts/speak"))).toBe(false);
});

test("Chat SSE 在 DONE 前 EOF 必须 reject", async ({ page }) => {
  await page.route(/\/(api\/)?intent\/route$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "chat_no_rag",
        confidence: 1,
        params: { text: "测试 Chat 截断" },
        rationale: "test",
      }),
    });
  });
  await page.route(/\/(api\/)?chat$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: ['data: {"delta":"截断的部分答案"}', "", ""].join("\n"),
    });
  });

  const mock = await installEchoMock(page, {
    skipPaths: ["/intent/route", "/chat", "/tts/speak"],
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const textarea = page.getByTestId("command-textarea");
  await textarea.fill("@chat 测试 Chat 截断");
  await textarea.press("Enter");

  await expect(
    page.locator(".ant-message-error").filter({ hasText: "暂时无法回复" }),
  ).toBeVisible({ timeout: 5_000 });
  await expect(
    page.getByTestId("assistant-message").filter({ hasText: "截断的部分答案" }),
  ).toHaveCount(0);
  await expect(
    page.locator(".ant-message-success").filter({ hasText: "已回复" }),
  ).toHaveCount(0);
  expect((await mock.fetchLog()).some((entry) => entry.url.includes("/tts/speak"))).toBe(false);
});

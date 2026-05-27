/**
 * E2E sad paths（M6 完整性闭环）。
 *
 * 覆盖 UI 在异常路径下的反馈是否友好，不崩、给提示、不卡死。
 *
 * 1. LLM 生成失败 → toast.error + textarea 可继续输入
 * 2. RAG 检索失败 → toast.error，UI 不崩
 * 3. 上传不支持文件类型 → toast.warning，不发请求
 *
 * （WS 断在 ws-reconnect.spec.ts 已覆盖，不再重复。）
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

const COMMAND_BAR_TA = "[data-testid='command-textarea']";

test("LLM 生成失败 → 错误 toast + textarea 不被锁死", async ({ page }) => {
  // intent route 走 page.route（默认 mock 没拦它，走 realFetch 会失败）
  await page.route("**/intent/route", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "generate_html",
        confidence: 0.95,
        params: { artifact_type: "html", brief: "测试 sad path" },
        rationale: "keyword",
      }),
    }),
  );

  // /artifacts/generate 注入 500
  await installEchoMock(page, {
    errorPaths: { "/artifacts/generate": 500 },
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill("@生成 HTML 测试 sad path");
  await ta.press("Enter");

  // 期望：error toast 显示生成失败
  await expect(
    page.locator(".ant-message-error").filter({ hasText: /生成失败/ }),
  ).toBeVisible({ timeout: 10_000 });

  // 期望：textarea 不被永久 disabled（fire-and-forget 模式应立即释放）
  await expect(ta).not.toBeDisabled({ timeout: 5_000 });

  // 期望：再次输入仍可工作（清空 + 重新输入）
  await ta.fill("继续输入测试");
  await expect(ta).toHaveValue("继续输入测试");
});

test("RAG 检索失败 → 错误 toast 不崩页", async ({ page }) => {
  await page.route("**/intent/route", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "search_rag",
        confidence: 0.95,
        params: { question: "未存在的问题" },
        rationale: "keyword",
      }),
    }),
  );
  await installEchoMock(page, {
    errorPaths: { "/rag/ask": 502 },
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  const ta = page.locator(COMMAND_BAR_TA);
  await ta.fill("@查 未存在的问题");
  await ta.press("Enter");

  await expect(
    page.locator(".ant-message-error").filter({ hasText: /检索失败/ }),
  ).toBeVisible({ timeout: 10_000 });

  // UI 仍能交互（顶部 brand 仍可见，textarea 仍可用）
  await expect(page.locator(".brand").filter({ hasText: /Echo/ })).toBeVisible();
  await expect(ta).not.toBeDisabled();
});

test("上传不支持的文件类型 → warning toast，不发起 ingest 请求", async ({ page }) => {
  const mock = await installEchoMock(page);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 准备一个不支持的扩展名文件（前端 ACCEPT_EXT_SET 拦截）
  const fileInput = page.getByTestId("command-file-input");
  await fileInput.setInputFiles({
    name: "binary.zzz",
    mimeType: "application/octet-stream",
    buffer: Buffer.from([0x00, 0x01, 0x02]),
  });

  // 期望：warning toast
  await expect(
    page.locator(".ant-message-warning").filter({ hasText: /不支持的文件类型/ }),
  ).toBeVisible({ timeout: 5_000 });

  // 期望：未发出 /rag/ingest 请求（前端拦截）
  const log = await mock.fetchLog();
  expect(log.some((r) => r.url.includes("/rag/ingest"))).toBeFalsy();

  // 期望：pending-docs 列表不显示该 chip
  await expect(
    page.getByTestId("pending-docs").locator(".ant-tag").filter({ hasText: /binary\.zzz/ }),
  ).toHaveCount(0);
});

test("上传后端拒绝（HTTP 400/500） → 错误 toast 不卡死 UI", async ({ page }) => {
  await installEchoMock(page, {
    errorPaths: { "/rag/ingest": 400 },
  });
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await page.getByTestId("command-file-input").setInputFiles({
    name: "huge.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# huge"),
  });

  await expect(
    page.locator(".ant-message-error").filter({ hasText: /入库失败/ }),
  ).toBeVisible({ timeout: 10_000 });

  // 入库中 spinner 应消失（uploading 计数回到 0）
  await expect(page.locator("text=/入库中 \\d+/")).toHaveCount(0, { timeout: 5_000 });

  // 不会留 chip
  await expect(
    page.getByTestId("pending-docs").locator(".ant-tag").filter({ hasText: /huge\.md/ }),
  ).toHaveCount(0);
});

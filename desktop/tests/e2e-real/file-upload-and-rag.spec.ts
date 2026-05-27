/**
 * 真后端 E2E：聊天框拖入 / 选择文件 → 入库 → workspace 状态栏计数变化 → @查命中。
 *
 * 覆盖 M6 用户需求：
 * 1. 文件直接扔在聊天框里读和问答（不是单独的上传 UI）
 * 2. 全场景通用（任意 markitdown 支持的格式都能入库）
 *
 * 不调慢路径 LLM（@查 的真实 LLM 回答路径放到 happy-path 套件）；
 * 这里只验证 ingest 链路 + 检索可命中（rag/ask 至少返回有 citations）。
 */
import { test, expect } from "@playwright/test";
import * as path from "node:path";
import * as fs from "node:fs";

function makeMdFile(content: string, name = "echo-test.md"): string {
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR ?? "/tmp", "echo-e2e-"));
  const p = path.join(dir, name);
  fs.writeFileSync(p, content, "utf-8");
  return p;
}

test("上传 markdown 文件 → workspace bar 显示 chip + 上传计数 +1", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  // 记录初始上传计数
  const wsBar = page.getByTestId("workspace-bar");
  await expect(wsBar).toBeVisible();
  const before = await wsBar.getByTestId("workspace-upload-count").innerText();
  const beforeUploads = parseInt(before.match(/上传 (\d+)/)?.[1] ?? "0", 10);

  // 通过 hidden file input 触发上传（更稳定，避免 drag-drop 模拟）
  const filePath = makeMdFile(
    "# Echo M6 测试笔记\n\n关键词：天王星 zaratoxin alphabet42\n这是用于真后端 E2E 验证拖入 RAG 链路的笔记。",
    "echo-m6-rag-test.md",
  );
  const fileInput = page.getByTestId("command-file-input");
  await fileInput.setInputFiles(filePath);

  // 期望：chip 出现 + 成功 toast
  const chip = page
    .getByTestId("pending-docs")
    .locator(".ant-tag")
    .filter({ hasText: /echo-m6-rag-test\.md/ });
  await expect(chip).toBeVisible({ timeout: 30_000 });

  // 上传计数 +1（WorkspaceBar 每 30s 轮询，但我们可触发手动刷新）
  // 简单等到下次轮询或直接查 stats
  await expect
    .poll(
      async () => {
        const txt = await wsBar.getByTestId("workspace-upload-count").innerText();
        return parseInt(txt.match(/上传 (\d+)/)?.[1] ?? "0", 10);
      },
      { timeout: 35_000, intervals: [1_000, 2_000, 5_000] },
    )
    .toBeGreaterThanOrEqual(beforeUploads + 1);
});

test("上传 md 后 RAG 检索命中（验证 retrieval 第一帧含目标 doc_id）", async ({ page }) => {
  test.setTimeout(180_000);

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 15_000 });

  // 上传一个 md 文件，含独特业务关键词
  const uniqueKey = "alphabet42echo";
  const filePath = makeMdFile(
    `# 真后端 RAG 命中测试\n\n业务关键词：${uniqueKey} 是 echo 团队 2026 自创术语，外网无任何引用。\n该笔记用于验证 ingest+query 链路通畅。`,
    `echo-rag-${Date.now()}.md`,
  );
  // 不走 hidden file input（更稳的是直接 HTTP 上传，避免依赖 UI 异步反馈）
  const apiBase = page.url().replace(/\/$/, "");
  const fileBuf = fs.readFileSync(filePath);
  const ingestResp = await page.request.post(`${apiBase}/api/rag/ingest`, {
    multipart: {
      file: {
        name: path.basename(filePath),
        mimeType: "text/markdown",
        buffer: fileBuf,
      },
    },
    timeout: 30_000,
  });
  expect(ingestResp.ok()).toBeTruthy();
  const { doc_id } = (await ingestResp.json()) as { doc_id: string };
  expect(doc_id).toMatch(/^md-/);

  // /rag/docs 应包含
  const docsResp = await page.request.get(`${apiBase}/api/rag/docs`);
  const docs = (await docsResp.json()) as { docs: Array<{ doc_id: string }> };
  expect(docs.docs.some((d) => d.doc_id === doc_id)).toBeTruthy();

  // /rag/ask SSE 第一帧应包含该 doc_id（retrieval phase 命中）
  const askResp = await page.request.post(`${apiBase}/api/rag/ask`, {
    data: { question: `${uniqueKey} 是什么？`, rag_top_k: 3, web_top_n: 0 },
    timeout: 150_000,
  });
  expect(askResp.status()).toBeLessThan(500);
  const sseText = await askResp.text();
  // retrieval 第一帧含 doc_id（chunks meta）；后续 LLM delta 是否复读关键词不强制
  expect(sseText).toContain(doc_id);
});

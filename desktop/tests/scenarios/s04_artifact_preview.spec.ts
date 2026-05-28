/**
 * 场景 4 扩展（P4.1 M4）：7 类产物 in-app 预览 + 顶栏清空 + 单条删除
 *
 * 设计：
 *  - 每个 sub-case 通过 publishArtifactReady() 注入对应 artifact_type 的 ws event
 *  - 对每个 artifact 单独 page.route() 拦截 /artifacts/<id>/download，
 *    返回真实可解析的 fixture（docx 来自硬盘，xlsx runtime 生成）
 *  - 点击 ArtifactPanel 上对应 card → 断言 Modal 内容
 *  - pptx 单独验证：不应弹 Modal，应触发 window.echo.openArtifactInSystem
 *
 * 范围限定（参考 M4 spec）：
 *  - 不验证 mammoth/SheetJS 解析的准确性（人家自家 unit test 覆盖了）
 *  - 只断言：Modal 出现 / loading 解除 / 关键 DOM 节点 / 调用次数
 */
import { readFileSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import * as XLSX from "xlsx";
import {
  installScenarioMock,
  publishArtifactReady,
  type EchoMock,
} from "./_helpers";

// Vite/Playwright 跑在 ESM 上下文 → 没有 __dirname。用 import.meta.url 自己算。
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const FIXTURE_DIR = path.join(__dirname, "fixtures");
const DOCX_BYTES = readFileSync(path.join(FIXTURE_DIR, "sample.docx"));

// ──────────── helpers ────────────

/**
 * Lazily build a 2×2 xlsx workbook in memory. SheetJS supports `write` with
 * type=buffer in node, giving us a real .xlsx that SheetJS.read() in the
 * browser can parse back.
 */
function buildSampleXlsx(): Buffer {
  const wb = XLSX.utils.book_new();
  const ws1 = XLSX.utils.aoa_to_sheet([
    ["指标", "Q1", "Q2"],
    ["营收", 100, 220],
    ["毛利", 35, 88],
  ]);
  const ws2 = XLSX.utils.aoa_to_sheet([
    ["sheet2 header", "value"],
    ["foo", 1],
  ]);
  XLSX.utils.book_append_sheet(wb, ws1, "财务");
  XLSX.utils.book_append_sheet(wb, ws2, "明细");
  return XLSX.write(wb, { bookType: "xlsx", type: "buffer" }) as Buffer;
}

/**
 * Register a Playwright network-level route for one artifact's download URL.
 * 必须在 publishArtifactReady 之前注册——预览 Modal 一打开就立刻 fetch。
 */
async function routeDownload(
  page: import("@playwright/test").Page,
  artifactId: string,
  body: Buffer | string,
  contentType: string,
): Promise<void> {
  await page.route(
    new RegExp(
      `/(api/)?artifacts/${artifactId.replace(/[-\\/\\\\^$*+?.()|[\\]{}]/g, "\\$&")}/download(\\?.*)?$`,
    ),
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": contentType },
        body,
      });
    },
  );
}

async function openArtifactCard(
  page: import("@playwright/test").Page,
  artifactId: string,
): Promise<void> {
  const card = page.locator(`[data-artifact-id="${artifactId}"]`);
  await expect(card).toBeVisible({ timeout: 5_000 });
  await card.click();
}

async function publishWithTitle(
  mock: EchoMock,
  artifactType: string,
  seq: number,
  id: string,
  title: string,
  filePath: string,
): Promise<void> {
  await publishArtifactReady(mock, artifactType, seq, id, title, filePath);
}

// ──────────── tests ────────────

test("S04a · markdown artifact → Modal 渲染 react-markdown", async ({ page }) => {
  const mock = await installScenarioMock(page);
  const id = "md-fixture-001";
  const md = `# 大标题\n\n这是一段 *斜体* 与 **粗体**。\n\n- 一\n- 二\n- 三\n`;
  await routeDownload(page, id, md, "text/markdown; charset=utf-8");

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "markdown", 1, id, "测试 Markdown", "/tmp/md.md");
  await openArtifactCard(page, id);

  const body = page.getByTestId("preview-markdown");
  await expect(body).toBeVisible({ timeout: 5_000 });
  await expect(body.locator("h1")).toHaveText("大标题");
  await expect(body.locator("li")).toHaveCount(3);
});

test("S04b · txt artifact → <pre> 渲染原文", async ({ page }) => {
  const mock = await installScenarioMock(page);
  const id = "txt-fixture-001";
  const txt = "line one\nline two\nline three\n";
  await routeDownload(page, id, txt, "text/plain; charset=utf-8");

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "txt", 1, id, "测试 TXT", "/tmp/x.txt");
  await openArtifactCard(page, id);

  const pre = page.getByTestId("preview-txt");
  await expect(pre).toBeVisible({ timeout: 5_000 });
  await expect(pre).toContainText("line one");
  await expect(pre).toContainText("line three");
});

test("S04c · pdf artifact → iframe 指向 download URL", async ({ page }) => {
  const mock = await installScenarioMock(page);
  const id = "pdf-fixture-001";
  // 最小 PDF magic bytes 即可（不会真渲染，只检查 iframe src 合规）
  await routeDownload(page, id, "%PDF-1.4\n%%EOF\n", "application/pdf");

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "pdf", 1, id, "测试 PDF", "/tmp/x.pdf");
  await openArtifactCard(page, id);

  const frame = page.getByTestId("preview-iframe-pdf");
  await expect(frame).toBeVisible({ timeout: 5_000 });
  await expect(frame).toHaveAttribute("src", new RegExp(`/${id}/download`));
});

test("S04d · docx artifact → mammoth 解析后 iframe 显示", async ({ page }) => {
  const mock = await installScenarioMock(page);
  const id = "docx-fixture-001";
  await routeDownload(
    page,
    id,
    DOCX_BYTES,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  );

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "word", 1, id, "测试 Word", "/tmp/x.docx");
  await openArtifactCard(page, id);

  // mammoth 解析需要 1-2s（CPU bound），先等 loading 出现，再等切走
  const docxFrame = page.getByTestId("preview-docx");
  await expect(docxFrame).toBeVisible({ timeout: 15_000 });
  // 不再有 loading；srcDoc 已注入
  await expect(docxFrame).toHaveAttribute("srcdoc", /<body>/);
});

test("S04e · xlsx artifact → SheetJS 解析 + tab 切换", async ({ page }) => {
  const mock = await installScenarioMock(page);
  const id = "xlsx-fixture-001";
  const xlsxBytes = buildSampleXlsx();
  await routeDownload(
    page,
    id,
    xlsxBytes,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  );

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "xlsx", 1, id, "测试 Excel", "/tmp/x.xlsx");
  await openArtifactCard(page, id);

  const xlsxPanel = page.getByTestId("preview-xlsx");
  await expect(xlsxPanel).toBeVisible({ timeout: 15_000 });
  // 2 个 sheet tab
  await expect(page.getByTestId("preview-xlsx-tab-0")).toHaveText("财务");
  await expect(page.getByTestId("preview-xlsx-tab-1")).toHaveText("明细");
  // sheet 1 默认 active；包含我们的单元格内容
  await expect(xlsxPanel).toContainText("营收");
  await expect(xlsxPanel).toContainText("220");

  // 切到 sheet 2
  await page.getByTestId("preview-xlsx-tab-1").click();
  await expect(xlsxPanel).toContainText("sheet2 header");
});

test("S04f · pptx artifact → 不开 Modal，走 openArtifactInSystem", async ({ page }) => {
  // 注入一个 spy 版的 openArtifactInSystem，记录调用参数
  await page.addInitScript(() => {
    const w = window as unknown as {
      __echoOpenCalls__: string[];
      echo?: Record<string, unknown>;
    };
    w.__echoOpenCalls__ = [];
    // 等 installScenarioMock 写完 window.echo 后再 wrap；用 setTimeout 0
    setTimeout(() => {
      if (w.echo) {
        const orig = w.echo as { openArtifactInSystem?: (p: string) => Promise<void> };
        orig.openArtifactInSystem = async (p: string) => {
          w.__echoOpenCalls__.push(p);
        };
      }
    }, 0);
  });
  const mock = await installScenarioMock(page);
  const id = "pptx-fixture-001";
  const expectedPath = "/tmp/sample.pptx";

  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "pptx", 1, id, "测试 PPTX", expectedPath);
  await openArtifactCard(page, id);

  // openArtifactInSystem 被调用，参数为 file_path
  await expect
    .poll(
      async () =>
        await page.evaluate(
          () => (window as unknown as { __echoOpenCalls__: string[] }).__echoOpenCalls__,
        ),
      { timeout: 5_000 },
    )
    .toContain(expectedPath);
  // 不应出现 Modal body
  await expect(page.getByTestId("preview-body")).toHaveCount(0);
});

test("S04g · 顶栏「清空 outputs」按钮 → confirm 后清空", async ({ page }) => {
  const mock = await installScenarioMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "markdown", 1, "a-1", "卡片 A", "/tmp/a.md");
  await publishWithTitle(mock, "txt", 2, "a-2", "卡片 B", "/tmp/b.txt");

  await expect(page.locator('[data-testid="artifact-card"]')).toHaveCount(2);

  await page.getByTestId("clear-artifacts-btn").click();
  // antd Modal.confirm — 「清空」按钮在 ant-modal-confirm-btns 里
  await page.locator(".ant-modal-confirm-btns .ant-btn-dangerous").click();

  await expect(page.locator('[data-testid="artifact-card"]')).toHaveCount(0);
  await expect(page.locator("text=暂无产物")).toBeVisible();
});

test("S04h · 单条 hover「×」按钮 → 仅删该条", async ({ page }) => {
  const mock = await installScenarioMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  await publishWithTitle(mock, "markdown", 1, "keep-1", "保留卡片", "/tmp/k.md");
  await publishWithTitle(mock, "txt", 2, "del-1", "待删卡片", "/tmp/d.txt");

  const cards = page.locator('[data-testid="artifact-card"]');
  await expect(cards).toHaveCount(2);

  // hover 第二条 → 点 × 按钮
  const targetCard = page.locator('[data-artifact-id="del-1"]');
  await targetCard.hover();
  await targetCard.getByTestId("remove-artifact-btn").click();

  await expect(cards).toHaveCount(1);
  await expect(page.locator('[data-artifact-id="keep-1"]')).toBeVisible();
  await expect(page.locator('[data-artifact-id="del-1"]')).toHaveCount(0);
});

test("S04i · 列表展示 title 主、artifact_id 副 / fallback", async ({ page }) => {
  const mock = await installScenarioMock(page);
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 5_000 });

  // 有 title 的卡片
  await publishWithTitle(
    mock,
    "markdown",
    1,
    "has-title-1",
    "FY26 Outlook 摘要",
    "/tmp/x.md",
  );
  // 无 title 的卡片（旧 backend 兼容）：直接 publish 不传 title 参数
  await publishArtifactReady(
    mock,
    "txt",
    2,
    "legacy-id-only-no-title-2",
    "", // empty title → fallback 到 artifact_id
  );

  const card1 = page.locator('[data-artifact-id="has-title-1"]');
  await expect(card1.getByTestId("artifact-title")).toHaveText("FY26 Outlook 摘要");

  const card2 = page.locator('[data-artifact-id="legacy-id-only-no-title-2"]');
  await expect(card2.getByTestId("artifact-title")).toHaveText(
    "legacy-id-only-no-title-2",
  );
});

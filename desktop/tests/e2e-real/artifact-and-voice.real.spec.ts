/**
 * 真链路 E2E：模拟用户操作 + 真云服务（STT / TTS / yunwu LLM / skill executor）。
 *
 * 不 mock 任何后端。前置条件：
 *   1. 真 backend 跑在 :8769（uvicorn app.main:app）
 *   2. dev server: VITE_API_TARGET=http://localhost:8769 npm run dev -- --port 5173
 *   3. 真 speech 样本（语音用例）: /tmp/echo_fixtures/wake_ppt.wav
 *      （由 backend/scripts/stress/make_speech_fixture.py 用真 TTS 合成）
 *
 * 覆盖：
 *   A. 用户在 CommandBar 打字 @生成 PPT / Excel / Word → 真 LLM 生成 → 产物卡片出现 + 文件可下载
 *   B. 完整语音指令：把真 speech WAV 通过 audioCapture 测试缝喂进 →
 *      真 STT 转写 → 唤醒词识别 → 真 LLM 生成 PPT → 产物落地（端到端语音→任务）
 *
 * 每个产物走真 LLM 60-180s，超时给到 8 分钟。
 */
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

const TA = "[data-testid='command-textarea']";
const WAKE_WAV = "/tmp/echo_fixtures/wake_ppt.wav";

async function gotoApp(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 20_000 });
  // ArtifactPanel mount 时会 listArtifacts(100) 预载历史产物；等它加载完，
  // 这样 snapshotArtifactIds 能拿到真实的"生成前"集合，避免误判历史卡片。
  await page.waitForTimeout(2_500);
}

/** 当前 outputs 面板里所有产物 id（生成前快照，用于排除历史卡片）。 */
async function snapshotArtifactIds(page: Page): Promise<Set<string>> {
  const ids = await page
    .getByTestId("artifact-card")
    .evaluateAll((els) =>
      els
        .map((el) => el.getAttribute("data-artifact-id"))
        .filter((x): x is string => Boolean(x)),
    );
  return new Set(ids);
}

/**
 * 等到出现一个**新**产物（不在 before 集合里）且类型徽标匹配，返回其 artifact_id。
 * 只认本次运行新生成的卡片，规避 ArtifactPanel 预载的历史产物造成的假阳性。
 */
async function waitForNewArtifactOfType(
  page: Page,
  before: Set<string>,
  badge: RegExp,
  timeout: number,
): Promise<string> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const cards = await page.getByTestId("artifact-card").evaluateAll((els) =>
      els.map((el) => ({
        id: el.getAttribute("data-artifact-id"),
        badge:
          el.querySelector("span.uppercase")?.textContent?.trim() ?? "",
      })),
    );
    const hit = cards.find(
      (c) => c.id && !before.has(c.id) && badge.test(c.badge),
    );
    if (hit?.id) return hit.id;
    await page.waitForTimeout(2_000);
  }
  throw new Error(`超时未出现新的 ${badge} 产物（${timeout}ms）`);
}

/** 真下载校验：产物文件真实落盘且非空。 */
async function assertDownloadable(page: Page, artifactId: string): Promise<void> {
  const resp = await page.request.get(
    `/api/artifacts/${encodeURIComponent(artifactId)}/download`,
  );
  expect(resp.status(), "下载应 200").toBe(200);
  const body = await resp.body();
  expect(body.byteLength, "产物文件应非空（>1KB）").toBeGreaterThan(1024);
}

test.describe("真链路 · 产物生成（打字）", () => {
  test("A1 · @生成 PPT → 真 LLM → pptx 产物可下载", async ({ page }) => {
    test.setTimeout(480_000);
    await gotoApp(page);
    const before = await snapshotArtifactIds(page);
    await page.locator(TA).fill("@生成 PPT 人工智能行业 2025 趋势 3 页");
    await page.locator(TA).press("Enter");
    const id = await waitForNewArtifactOfType(page, before, /^pptx?$/i, 420_000);
    await assertDownloadable(page, id);
  });

  test("A2 · @生成 Excel → 真 LLM → xlsx 产物可下载", async ({ page }) => {
    test.setTimeout(480_000);
    await gotoApp(page);
    const before = await snapshotArtifactIds(page);
    await page.locator(TA).fill("@生成 Excel 2024 四个季度营收对比表");
    await page.locator(TA).press("Enter");
    const id = await waitForNewArtifactOfType(page, before, /^xlsx$/i, 420_000);
    await assertDownloadable(page, id);
  });

  test("A3 · @生成 Word → 真 LLM → word 产物可下载", async ({ page }) => {
    test.setTimeout(480_000);
    await gotoApp(page);
    const before = await snapshotArtifactIds(page);
    await page.locator(TA).fill("@生成 Word AI Agent 简短调研 两段");
    await page.locator(TA).press("Enter");
    const id = await waitForNewArtifactOfType(page, before, /^(word|docx)$/i, 420_000);
    await assertDownloadable(page, id);
  });
});

test.describe("真链路 · 完整语音指令完成任务", () => {
  test("B1 · 真 speech → 真 STT → 唤醒 → 真 LLM → PPT 落地", async ({ page }) => {
    test.setTimeout(480_000);
    await gotoApp(page);
    const before = await snapshotArtifactIds(page);

    // 把真 TTS 合成的 speech WAV 读进来，转 base64 注入 page，
    // 通过 audioCapture 测试缝喂进真实 captureChunkRouter（= 真实麦克风输入的等价物）。
    const wavBase64 = readFileSync(WAKE_WAV).toString("base64");

    // 等 audioCapture 测试缝就绪（dev build 暴露 window.__echoAudioCapture）
    await page.waitForFunction(
      () =>
        Boolean(
          (window as unknown as { __echoAudioCapture?: unknown })
            .__echoAudioCapture,
        ),
      undefined,
      { timeout: 10_000 },
    );

    await page.evaluate((b64: string) => {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const blob = new Blob([bytes], { type: "audio/wav" });
      const cap = (
        window as unknown as {
          __echoAudioCapture: { __emitChunkForTest: (b: Blob) => void };
        }
      ).__echoAudioCapture;
      cap.__emitChunkForTest(blob);
    }, wavBase64);

    // 语音 @echo 不再造右侧"用户"气泡（用户说的话已在左侧转写流），
    // 改为直接出现一条左侧 Echo 回复气泡（pending → done）。
    await expect(
      page.getByTestId("conv-bubble-assistant_reply").first(),
    ).toBeVisible({ timeout: 60_000 });

    // 真 LLM 生成 PPT → 产物出现在 outputs 面板并可下载（必须是本次新生成）
    const id = await waitForNewArtifactOfType(page, before, /^pptx?$/i, 420_000);
    await assertDownloadable(page, id);
  });
});

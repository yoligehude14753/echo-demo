/**
 * 真云服务 E2E：
 * - Playwright 模拟用户点击 CommandBar 生成 PPT / Excel / Word
 * - 用真实 TTS 合成语音，再注入前端采集链路，经过真实 STT 后触发语音 Echo 指令
 *
 * 不使用 fetch / WS mock。需要 backend、Vite dev server 和云服务健康。
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

type ArtifactType = "pptx" | "xlsx" | "word";

interface ArtifactRow {
  artifact_id: string;
  artifact_type: string;
  title: string;
  file_path: string;
  size_bytes: number;
}

const COMMANDS: Array<{ type: ArtifactType; text: string; uiLabel: string }> = [
  {
    type: "pptx",
    text: "@生成 PPT EchoDesk 真云服务验收，3 页，包含目标、方案、下一步，不要投资分析和目标价",
    uiLabel: "PPTX",
  },
  {
    type: "xlsx",
    text: "@生成 Excel EchoDesk 真云服务验收表，三行数据：PPT、Excel、Word，列为功能、状态、备注",
    uiLabel: "XLSX",
  },
  {
    type: "word",
    text: "@生成 Word EchoDesk 真云服务验收报告，写两段，说明 PPT Excel Word 都已验证",
    uiLabel: "WORD",
  },
];

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("echodesk.onboarding.completed", "1");
    window.localStorage.setItem("echodesk.tts.enabled", "0");
    window.localStorage.setItem("echodesk.tts.defaultOffMigrated", "1");
  });
});

async function gotoApp(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 30_000 });
  const ambient = page.getByTestId("meeting-item-ambient");
  if (await ambient.isVisible().catch(() => false)) {
    await ambient.click();
  }
}

async function listArtifacts(request: APIRequestContext, baseURL: string): Promise<ArtifactRow[]> {
  const resp = await request.get(`${baseURL}/api/artifacts?limit=200`, { timeout: 30_000 });
  expect(resp.ok()).toBeTruthy();
  return (await resp.json()) as ArtifactRow[];
}

async function countArtifacts(
  request: APIRequestContext,
  baseURL: string,
  type: ArtifactType,
): Promise<number> {
  const rows = await listArtifacts(request, baseURL);
  return rows.filter((a) => a.artifact_type === type).length;
}

async function sendByClick(page: Page, text: string): Promise<void> {
  const textarea = page.getByTestId("command-textarea");
  await expect(textarea).toBeEnabled({ timeout: 30_000 });
  await textarea.fill(text);
  await page.getByTestId("command-send-btn").click();
}

async function waitForNewArtifact(
  page: Page,
  type: ArtifactType,
  uiLabel: string,
  before: number,
): Promise<void> {
  const baseURL = "http://localhost:5173";
  await expect
    .poll(() => countArtifacts(page.request, baseURL, type), {
      timeout: 900_000,
      intervals: [5_000, 10_000, 15_000],
    })
    .toBeGreaterThan(before);
  await expect(page.getByTestId("artifact-card").filter({ hasText: uiLabel }).first()).toBeVisible({
    timeout: 60_000,
  });
}

function pcm16ToWav(pcm: Buffer, sampleRate = 16_000): Buffer {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

for (const item of COMMANDS) {
  test(`真实点击生成 ${item.uiLabel}`, async ({ page }) => {
    test.setTimeout(1_200_000);
    await gotoApp(page);

    const before = await countArtifacts(page.request, "http://localhost:5173", item.type);
    const failedBefore = await page.getByTestId("failed-artifact-card").count();
    await sendByClick(page, item.text);
    await expect(page.getByTestId("failed-artifact-card")).toHaveCount(failedBefore, {
      timeout: 1_000,
    });
    await waitForNewArtifact(page, item.type, item.uiLabel, before);
  });
}

test("真实点击连续生成 PPT / Excel / Word", async ({ page }) => {
  test.setTimeout(1_800_000);
  await gotoApp(page);

  for (const item of COMMANDS) {
    const before = await countArtifacts(page.request, "http://localhost:5173", item.type);
    await sendByClick(page, item.text);
    await waitForNewArtifact(page, item.type, item.uiLabel, before);
  }
});

test("真实语音指令：TTS 音频 → STT → Echo agent → Word 产物", async ({ page }) => {
  test.setTimeout(1_200_000);
  await gotoApp(page);

  await expect
    .poll(
      () =>
        page.evaluate(() =>
          Boolean((window as Window & { __echoAudioCapture?: unknown }).__echoAudioCapture),
        ),
      { timeout: 30_000 },
    )
    .toBe(true);

  const before = await countArtifacts(page.request, "http://localhost:5173", "word");
  const voiceRunId = Date.now().toString().slice(-6);
  const voiceText = `爱口，生成 Word 语音验收报告，写一段说明，编号 ${voiceRunId}。`;
  let speakResp = await page.request.post("http://localhost:5173/api/tts/speak", {
    data: {
      text: voiceText,
    },
    timeout: 240_000,
  });
  if (!speakResp.ok()) {
    await page.waitForTimeout(5_000);
    speakResp = await page.request.post("http://localhost:5173/api/tts/speak", {
      data: {
        text: voiceText,
      },
      timeout: 240_000,
    });
  }
  expect(speakResp.ok()).toBeTruthy();
  const pcm = await speakResp.body();
  expect(pcm.length).toBeGreaterThan(1_000);
  const wav = pcm16ToWav(pcm);

  await page.evaluate((bytes) => {
    const blob = new Blob([new Uint8Array(bytes)], { type: "audio/wav" });
    (
      window as Window & {
        __echoAudioCapture?: { __emitChunkForTest?: (blob: Blob) => void };
      }
    ).__echoAudioCapture?.__emitChunkForTest?.(blob);
  }, Array.from(wav));

  await expect(page.getByTestId("transcript-scroller")).toContainText(/echo生成word|iqoo生成word/i, {
    timeout: 180_000,
  });
  await waitForNewArtifact(page, "word", "WORD", before);
});

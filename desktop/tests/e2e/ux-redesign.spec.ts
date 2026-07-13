import { expect, test } from "@playwright/test";
import {
  installEchoMock,
  publishArtifactReady,
} from "./_mock";

test("新版工作台：统一系统字体、清晰分区，长输入自动增高且不横向溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const conversationMode = page.getByTestId("conversation-mode");
  const minutesTab = page.getByTestId("inspector-tab-minutes");
  const artifactsTab = page.getByTestId("inspector-tab-artifacts");
  const textarea = page.getByTestId("command-textarea");

  await expect(conversationMode).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("conversation-source-transcript")).toBeVisible();
  await expect(page.getByTestId("conversation-source-ai")).toBeVisible();
  await expect(page.getByTestId("workspace-view-transcript")).toHaveCount(0);
  await expect(page.getByTestId("workspace-view-assistant")).toHaveCount(0);
  await expect(minutesTab).toBeVisible();
  await expect(artifactsTab).toBeVisible();
  await expect(page.locator("#workspace-stream-view")).toHaveAttribute(
    "data-view",
    "conversation",
  );

  const eventTs = new Date().toISOString();
  await page.evaluate(async (ts) => {
    const { useStore } = await import("/src/store.ts");
    const applyEvent = useStore.getState().applyEvent;
    applyEvent({
      type: "rag.query",
      seq: 11,
      ts,
      meeting_id: null,
      payload: { question: "统一对话流问题" },
    });
    applyEvent({
      type: "rag.answer.done",
      seq: 12,
      ts: new Date(Date.parse(ts) + 1).toISOString(),
      meeting_id: null,
      payload: { answer: "统一对话流回答" },
    });
  }, eventTs);
  const scroller = page.getByTestId("transcript-scroller");
  await expect(scroller.getByText("统一对话流问题")).toBeVisible();
  await expect(scroller.getByText("统一对话流回答")).toBeVisible();
  await expect(page.getByText("Echo AI")).toBeVisible();
  const rowBorders = await page.getByTestId("transcript-row").evaluateAll((rows) =>
    rows.map((row) => getComputedStyle(row).borderBottomWidth),
  );
  expect(rowBorders.every((width) => width === "0px")).toBe(true);

  await minutesTab.click();
  await expect(minutesTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#inspector-minutes")).toBeVisible();
  await expect(page.locator("#inspector-artifacts")).toBeHidden();
  await artifactsTab.click();
  await expect(artifactsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#inspector-artifacts")).toBeVisible();

  const longArtifactTitle = `超长产物标题${"LongTokenWithoutAnyBreak".repeat(18)}`;
  await publishArtifactReady(
    mock,
    "txt",
    1,
    "internal-artifact-id-that-must-stay-hidden",
    longArtifactTitle,
  );
  const longArtifactCard = page.locator(
    '[data-artifact-id="internal-artifact-id-that-must-stay-hidden"]',
  );
  await expect(longArtifactCard).toBeVisible();
  await expect(longArtifactCard).not.toContainText("internal-artifact-id");

  const typography = await page.evaluate(() => {
    const family = (selector: string) => {
      const element = document.querySelector(selector);
      return element ? getComputedStyle(element).fontFamily : null;
    };
    return {
      body: getComputedStyle(document.body).fontFamily,
      conversationMode: family("[data-testid='conversation-mode']"),
      inspectorTab: family("[data-testid='inspector-tab-minutes']"),
      textarea: family("[data-testid='command-textarea']"),
    };
  });

  // Chromium 会把 BlinkMacSystemFont 规范化为 system-ui；两者都表示浏览器的
  // 平台系统字体入口。
  expect(typography.body).toMatch(
    /-apple-system.*(?:BlinkMacSystemFont|system-ui).*Segoe UI/,
  );
  expect(typography.body).toContain("Segoe UI");
  expect(typography.body).toContain("PingFang SC");
  expect(typography.body).not.toContain("Inter");
  expect(typography.conversationMode).toBe(typography.body);
  expect(typography.inspectorTab).toBe(typography.body);
  expect(typography.textarea).toBe(typography.body);

  const initialHeight = await textarea.evaluate((element) =>
    element.getBoundingClientRect().height,
  );
  const longText = Array.from(
    { length: 8 },
    (_, index) =>
      `${index + 1}. 请基于当前会议内容整理一份完整行动清单并标注负责人和截止时间，LongTokenWithoutAnyBreak${"X".repeat(60)}`,
  ).join("\n");
  await textarea.fill(longText);

  await expect
    .poll(() =>
      textarea.evaluate((element) => element.getBoundingClientRect().height),
    )
    .toBeGreaterThan(initialHeight);

  const overflow = await page.evaluate(() => {
    const input = document.querySelector<HTMLTextAreaElement>(
      "[data-testid='command-textarea']",
    );
    if (!input) throw new Error("command textarea missing");
    const rect = input.getBoundingClientRect();
    return {
      inputHeight: rect.height,
      inputClientWidth: input.clientWidth,
      inputScrollWidth: input.scrollWidth,
      overflowX: getComputedStyle(input).overflowX,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      artifactWidth: document
        .querySelector('[data-artifact-id="internal-artifact-id-that-must-stay-hidden"]')
        ?.getBoundingClientRect().width ?? 0,
      inspectorWidth: document
        .querySelector("[data-testid='inspector']")
        ?.getBoundingClientRect().width ?? 0,
    };
  });

  expect(overflow.inputHeight).toBeLessThanOrEqual(145);
  expect(overflow.inputScrollWidth).toBeLessThanOrEqual(
    overflow.inputClientWidth + 1,
  );
  expect(overflow.overflowX).toBe("hidden");
  expect(overflow.documentWidth).toBeLessThanOrEqual(
    overflow.viewportWidth + 1,
  );
  expect(overflow.artifactWidth).toBeLessThanOrEqual(overflow.inspectorWidth);
});

test("会议纪要：历史纪要集中展示并可快速切换", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    const applyEvent = useStore.getState().applyEvent;
    const minutes = (meetingId: string, title: string) => ({
      meeting_id: meetingId,
      title,
      duration_sec: 60,
      speakers: ["说话人1"],
      summary: `${title}的摘要内容`,
      sections: [{ heading: "议题", bullets: ["要点"] }],
      decisions: [],
      todos: [],
      action_items: [],
      created_at: new Date().toISOString(),
    });
    applyEvent({
      type: "meeting.started",
      seq: 21,
      ts: new Date().toISOString(),
      meeting_id: "meeting-history-a",
      payload: {},
    });
    applyEvent({
      type: "minutes.ready",
      seq: 22,
      ts: new Date().toISOString(),
      meeting_id: "meeting-history-a",
      payload: minutes("meeting-history-a", "第一场测试纪要"),
    });
    applyEvent({
      type: "meeting.started",
      seq: 23,
      ts: new Date().toISOString(),
      meeting_id: "meeting-history-b",
      payload: {},
    });
    applyEvent({
      type: "minutes.ready",
      seq: 24,
      ts: new Date().toISOString(),
      meeting_id: "meeting-history-b",
      payload: minutes("meeting-history-b", "第二场测试纪要"),
    });
  });

  const minutesTab = page.getByTestId("inspector-tab-minutes");
  await minutesTab.click();
  const history = page.getByTestId("minutes-history");
  await expect(history).toBeVisible();
  await expect(history.getByTestId("minutes-history-item")).toHaveCount(2);

  const firstMeeting = history.locator(
    '[data-testid="minutes-history-item"][data-history-meeting-id="meeting-history-a"]',
  );
  await firstMeeting.click();
  await expect(firstMeeting).toHaveAttribute("aria-current", "page");
  await expect(page.getByTestId("minutes-title")).toContainText("第一场测试纪要");
});

test("新版工作台：960 宽度下检查器可作为抽屉打开并从内部收起", async ({
  page,
}) => {
  await page.setViewportSize({ width: 960, height: 720 });
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const toggle = page.getByTestId("inspector-toggle");
  const inspector = page.getByTestId("inspector");
  const close = page.getByTestId("inspector-close");

  await expect(toggle).toBeVisible({ timeout: 10_000 });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(inspector).toBeHidden();

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(inspector).toHaveClass(/is-open/);
  await expect(inspector).toBeVisible();
  await expect(close).toBeVisible();
  await expect(close).toHaveAccessibleName("收起检查器");
  await expect
    .poll(
      async () => {
        const box = await inspector.boundingBox();
        return box ? box.x + box.width : Number.POSITIVE_INFINITY;
      },
      { message: "检查器打开动画结束后右边缘应留在视口内" },
    )
    .toBeLessThanOrEqual(961);

  const layout = await page.evaluate(() => {
    const inspectorElement = document.querySelector("[data-testid='inspector']");
    const transcriptElement = document.querySelector(".echodesk-transcript-pane");
    if (!inspectorElement || !transcriptElement) {
      throw new Error("workspace layout missing");
    }
    const inspectorRect = inspectorElement.getBoundingClientRect();
    const transcriptRect = transcriptElement.getBoundingClientRect();
    return {
      inspectorLeft: inspectorRect.left,
      inspectorRight: inspectorRect.right,
      inspectorWidth: inspectorRect.width,
      transcriptWidth: transcriptRect.width,
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  });

  expect(layout.inspectorLeft).toBeGreaterThanOrEqual(599);
  expect(layout.inspectorRight).toBeLessThanOrEqual(layout.viewportWidth + 1);
  expect(layout.inspectorWidth).toBeLessThanOrEqual(360);
  expect(layout.transcriptWidth).toBeGreaterThanOrEqual(700);
  expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
  await expect(page.getByTestId("inspector-tab-minutes")).toBeVisible();
  await expect(page.getByTestId("inspector-tab-artifacts")).toBeVisible();

  await close.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();
  await expect(inspector).not.toHaveClass(/is-open/);
  await expect(inspector).toBeHidden();
  await expect
    .poll(
      () =>
        inspector.evaluate((element) => element.getBoundingClientRect().left),
      { message: "收起后检查器应完整离开视口" },
    )
    .toBeGreaterThanOrEqual(958);
});

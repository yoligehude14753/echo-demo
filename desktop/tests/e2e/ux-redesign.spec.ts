import { expect, test } from "@playwright/test";
import { installEchoMock, publishArtifactReady } from "./_mock";

test("新版工作台：统一系统字体、清晰分区，长输入自动增高且不横向溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const transcriptTab = page.getByTestId("workspace-view-transcript");
  const assistantTab = page.getByTestId("workspace-view-assistant");
  const minutesTab = page.getByTestId("inspector-tab-minutes");
  const artifactsTab = page.getByTestId("inspector-tab-artifacts");
  const textarea = page.getByTestId("command-textarea");

  await expect(transcriptTab).toBeVisible({ timeout: 10_000 });
  await expect(assistantTab).toBeVisible();
  await expect(minutesTab).toBeVisible();
  await expect(artifactsTab).toBeVisible();
  await expect(transcriptTab).toHaveAttribute("aria-selected", "true");

  await assistantTab.click();
  await expect(assistantTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#workspace-stream-view")).toHaveAttribute(
    "data-view",
    "assistant",
  );
  await transcriptTab.click();
  await expect(transcriptTab).toHaveAttribute("aria-selected", "true");

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
      transcriptTab: family("[data-testid='workspace-view-transcript']"),
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
  expect(typography.transcriptTab).toBe(typography.body);
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

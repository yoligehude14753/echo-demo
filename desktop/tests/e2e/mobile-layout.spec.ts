import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("移动视口：主工作区不被 AntD Sider 布局压成 0 宽", async ({ page }) => {
  await page.setViewportSize({ width: 411, height: 866 });
  await installEchoMock(page);

  await page.goto("/");

  await expect(page.getByText("转写流")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("会议纪要", { exact: true })).toBeVisible();
  await expect(page.getByTestId("command-bar")).toBeVisible();

  const boxes = await page.evaluate(() => {
    const pick = (selector: string) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    };
    return {
      content: pick(".echodesk-content"),
      transcript: pick(".echodesk-transcript-pane"),
      output: pick(".echodesk-output-pane"),
      commandBar: pick("[data-testid='command-bar']"),
      sider: pick(".echodesk-meeting-sider"),
    };
  });

  expect(boxes.content?.width).toBeGreaterThan(300);
  expect(boxes.transcript?.width).toBeGreaterThan(300);
  expect(boxes.output?.width).toBeGreaterThan(300);
  expect(boxes.commandBar?.width).toBeGreaterThan(300);
  expect(boxes.sider?.width ?? 0).toBe(0);
});

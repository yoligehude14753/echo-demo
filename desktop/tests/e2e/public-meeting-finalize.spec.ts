import { expect, test, type Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

async function installPublicRuntime(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
    };
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 2, appVersion: "test" }),
    );
  });
  await page.route(/\/(api\/)?meetings\/current$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "idle", meeting_id: null }),
    }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/transcript$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/artifacts$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/minutes$/, (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" }),
  );
}

function meetingActionPaths(
  log: Array<{ url: string; method: string }>,
): string[] {
  return log
    .filter(({ method, url }) => method === "POST" && url.includes("/meetings/"))
    .map(({ url }) => url.replace(/^https?:\/\/[^/]+/, "").replace(/^\/api/, ""));
}

test("public/TV 结束会议会 end 后 finalize，并把纪要写回界面", async ({ page }) => {
  await installPublicRuntime(page);
  await page.route(/\/(api\/)?meetings\/[^/]+\/start$/, (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/end$/, (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.route(/\/(api\/)?meetings\/([^/]+)\/finalize$/, async (route) => {
    const meetingId = new URL(route.request().url()).pathname.split("/").at(-2) ?? "local";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        meeting_id: meetingId,
        title: "本机会议纪要",
        duration_sec: 10,
        speakers: [],
        summary: "public finalize 已完成",
        sections: [],
        decisions: [],
        action_items: [],
        created_at: new Date().toISOString(),
      }),
    });
  });
  const mock = await installEchoMock(page, { skipPaths: ["/meetings/"] });
  await page.goto("/");

  const status = page.getByTestId("meeting-status-bar");
  await status.click();
  await expect(status).toContainText("会议中");
  await status.click();

  await expect(page.getByText("已结束本机会议并生成纪要")).toBeVisible();
  await expect(page.getByText("public finalize 已完成")).toBeVisible();
  const actions = meetingActionPaths(await mock.fetchLog());
  expect(actions).toHaveLength(3);
  expect(actions[0]).toMatch(/\/start$/);
  expect(actions[1]).toMatch(/\/end$/);
  expect(actions[2]).toMatch(/\/finalize$/);
});

test("public/TV finalize 失败可见，且不伪装结束成功", async ({ page }) => {
  await installPublicRuntime(page);
  await page.route(/\/(api\/)?meetings\/[^/]+\/start$/, (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/end$/, (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.route(/\/(api\/)?meetings\/[^/]+\/finalize$/, (route) =>
    route.fulfill({ status: 500, body: "minutes failed" }),
  );
  const mock = await installEchoMock(page, { skipPaths: ["/meetings/"] });
  await page.goto("/");

  const status = page.getByTestId("meeting-status-bar");
  await status.click();
  await expect(status).toContainText("会议中");
  await status.click();

  await expect(
    page.locator(".ant-message-error").filter({
      hasText: "会议已结束，但纪要生成失败，请在纪要面板重试",
    }),
  ).toBeVisible();
  await expect(page.getByText("已结束本机会议并生成纪要")).toHaveCount(0);
  const actions = meetingActionPaths(await mock.fetchLog());
  expect(actions).toHaveLength(3);
  expect(actions[2]).toMatch(/\/finalize$/);
});

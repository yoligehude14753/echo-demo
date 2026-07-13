import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("meeting list distinguishes loading from a successful empty response", async ({ page }) => {
  let pendingRoute: import("@playwright/test").Route | null = null;
  let signalRequest!: () => void;
  const requested = new Promise<void>((resolve) => {
    signalRequest = resolve;
  });
  await installEchoMock(page, { skipPaths: ["/meetings?"] });
  await page.route(/\/(api\/)?meetings\?/, (route) => {
    pendingRoute = route;
    signalRequest();
  });

  await page.goto("/");
  await requested;
  await expect(page.getByTestId("meeting-list-loading")).toBeVisible();
  await expect(page.getByTestId("meeting-list-empty")).toHaveCount(0);

  await pendingRoute!.fulfill({
    status: 200,
    contentType: "application/json",
    body: "[]",
  });
  await expect(page.getByTestId("meeting-list-empty")).toBeVisible();
  await expect(page.getByTestId("meeting-list-loading")).toHaveCount(0);
});

test("six failed attempts show an actionable error and retry can recover to empty", async ({
  page,
}) => {
  test.setTimeout(30_000);
  let mode: "error" | "empty" = "error";
  let attempts = 0;
  await installEchoMock(page, { skipPaths: ["/meetings?"] });
  await page.route(/\/(api\/)?meetings\?/, (route) => {
    attempts += 1;
    return route.fulfill({
      status: mode === "error" ? 500 : 200,
      contentType: "application/json",
      body: mode === "error" ? JSON.stringify({ detail: "temporary failure" }) : "[]",
    });
  });

  await page.goto("/");
  await expect(page.getByTestId("meeting-list-error")).toBeVisible({ timeout: 22_000 });
  expect(attempts).toBe(6);
  await expect(page.getByTestId("meeting-list-empty")).toHaveCount(0);
  await expect(page.getByTestId("meeting-list-error")).toContainText(
    "历史会议暂时无法加载",
  );

  mode = "empty";
  await page.getByTestId("retry-meeting-list").click();
  await expect(page.getByTestId("meeting-list-empty")).toBeVisible();
  expect(attempts).toBe(7);
});

test("cached meetings remain visible in degraded and resync-loading states", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");
  await expect(page.getByTestId("meeting-list-empty")).toBeVisible();

  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    const store = useStore.getState();
    store.upsertMeeting("cached-meeting", {
      title: "上次同步保留的会议",
      state: "ended",
      started_at: "2026-07-12T02:00:00Z",
      ended_at: "2026-07-12T02:10:00Z",
    });
    store.completeMeetingListLoad();
    store.failMeetingListLoad("历史会议暂时无法加载，请检查服务连接后重试");
  });

  await expect(page.getByText("上次同步保留的会议")).toBeVisible();
  await expect(page.getByTestId("meeting-list-degraded")).toBeVisible();
  await expect(page.getByTestId("meeting-list-empty")).toHaveCount(0);

  await page.evaluate(async () => {
    const { useStore } = await import("/src/store.ts");
    useStore.getState().startMeetingListLoad();
  });
  await expect(page.getByTestId("meeting-list-loading-cached")).toBeVisible();
  await expect(page.getByText("上次同步保留的会议")).toBeVisible();
});

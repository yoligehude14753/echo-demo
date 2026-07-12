import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("server_resync triggers REST rehydrate without overwriting newer WS state", async ({
  page,
}) => {
  const mock = await installEchoMock(page, { skipPaths: ["/meetings?"] });
  let listCalls = 0;
  await page.route(/\/(api\/)?meetings\?limit=/, async (route) => {
    listCalls += 1;
    if (listCalls === 1) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 250));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          meeting_id: "meeting-resync",
          title: "旧快照",
          display_title: null,
          state: "ended",
          started_at: "2026-07-11T00:00:00Z",
          ended_at: "2026-07-11T00:01:00Z",
          finalized_at: null,
          n_segments: 0,
          n_speakers: 0,
          has_minutes: false,
        },
      ]),
    });
  });

  await page.goto("/");
  await expect.poll(() => listCalls).toBe(1);
  await mock.publish({
    type: "server_resync",
    seq: 0,
    stream_epoch: "epoch-resync",
    ts: new Date().toISOString(),
    payload: {
      reason: "history_expired",
      fence_seq: 9,
      stream_epoch: "epoch-resync",
    },
  });
  await expect.poll(() => listCalls).toBe(2);
  await mock.publish({
    type: "server_sync",
    seq: 9,
    stream_epoch: "epoch-resync",
    ts: new Date().toISOString(),
    payload: {
      strategy: "replace",
      fence_seq: 9,
      stream_epoch: "epoch-resync",
    },
  });
  await mock.publish({
    type: "meeting.started",
    seq: 10,
    stream_epoch: "epoch-resync",
    ts: new Date().toISOString(),
    meeting_id: "meeting-resync",
    payload: { title: "新事件" },
  });

  const item = page.locator('[data-testid="meeting-item"][data-meeting-id="meeting-resync"]');
  await expect(item).toContainText("进行中");
  await expect(item).not.toContainText("已结束");
});

test("WS cursor persists only epoch and seq and is sent on reload", async ({ page }) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await mock.publish({
    type: "server_hello",
    seq: 0,
    stream_epoch: "epoch-persisted",
    ts: new Date().toISOString(),
    payload: { max_seq: 0, stream_epoch: "epoch-persisted", version: "1.0" },
  });
  for (let seq = 1; seq <= 7; seq += 1) {
    await mock.publish({
      type: "chat.done",
      seq,
      stream_epoch: "epoch-persisted",
      ts: new Date().toISOString(),
      payload: {},
    });
  }

  await expect
    .poll(() =>
      page.evaluate(() =>
        Object.entries(window.localStorage).some(
          ([key, value]) =>
            key.startsWith("echodesk.wsCursor.v1:") &&
            value.includes("epoch-persisted") &&
            value.includes('"seq":7'),
        ),
      ),
    )
    .toBe(true);

  await page.reload();
  await expect
    .poll(async () => {
      const frames = await mock.wsSent();
      return frames.map((frame) => JSON.parse(frame) as Record<string, unknown>);
    })
    .toContainEqual(
      expect.objectContaining({
        type: "client_hello",
        last_seq: 7,
        stream_epoch: "epoch-persisted",
      }),
    );

  const storedValues = await page.evaluate(() => Object.values(window.localStorage));
  expect(storedValues.join("\n")).not.toContain("mock-session-token");
});

test("non-contiguous WS event is rejected before it can corrupt UI state", async ({
  page,
}) => {
  const mock = await installEchoMock(page);
  await page.goto("/");
  await expect(page.getByText("已连接", { exact: true })).toBeVisible();

  await mock.publish({
    type: "meeting.started",
    seq: 2,
    stream_epoch: "epoch-gap",
    ts: new Date().toISOString(),
    meeting_id: "meeting-missing-seq-one",
    payload: { title: "不应被应用" },
  });

  await expect(page.getByText("断线", { exact: true })).toBeVisible();
  await expect(
    page.locator(
      '[data-testid="meeting-item"][data-meeting-id="meeting-missing-seq-one"]',
    ),
  ).toHaveCount(0);
  const cursorValues = await page.evaluate(() =>
    Object.entries(window.localStorage)
      .filter(([key]) => key.startsWith("echodesk.wsCursor.v1:"))
      .map(([, value]) => value),
  );
  expect(cursorValues.join("\n")).not.toContain('"seq":2');
});

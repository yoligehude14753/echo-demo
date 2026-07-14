import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("Hub runtime paired status is reflected in the sync indicator", async ({ page }) => {
  await installEchoMock(page);
  await page.goto("/");

  await expect(page.getByTestId("sync-status")).toContainText("已同步", {
    timeout: 5_000,
  });
  await expect(page.getByTestId("sync-status")).not.toContainText("未配对");
});

test("sync indicator refreshes after an initial Hub status failure", async ({ page }) => {
  await installEchoMock(page);
  await page.addInitScript(() => {
    let hubStatusCalls = 0;
    const mockedFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/hub/status") && hubStatusCalls++ === 0) {
        return new Response(JSON.stringify({ detail: "backend starting" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return mockedFetch(input, init);
    };
  });
  await page.goto("/");

  await expect(page.getByTestId("sync-status")).toContainText("已同步", {
    timeout: 5_000,
  });
});

test("paired display survives a transient Hub status refresh failure", async ({ page }) => {
  await installEchoMock(page);
  await page.addInitScript(() => {
    let hubStatusCalls = 0;
    const mockedFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/hub/status") && hubStatusCalls++ === 1) {
        return new Response(JSON.stringify({ detail: "temporary status failure" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return mockedFetch(input, init);
    };
  });
  await page.goto("/");
  await expect(page.getByTestId("sync-status")).toContainText("已同步", {
    timeout: 5_000,
  });

  await page.evaluate(() => {
    window.dispatchEvent(new Event("echodesk:sync-hub-change"));
  });
  await expect(page.getByTestId("sync-status")).toContainText("已同步");
});

test("an active meeting loaded from history is selected for transcript display", async ({ page }) => {
  const meetingId = "gateway-meeting";
  const marker = "SYNC_UI_HISTORY_MARKER";
  await installEchoMock(page, {
    skipPaths: ["/meetings?", `/meetings/${meetingId}/transcript`],
  });
  await page.route("**/meetings?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          meeting_id: meetingId,
          title: null,
          display_title: "Gateway meeting",
          state: "in_meeting",
          started_at: "2026-07-15T00:00:00.000Z",
          ended_at: null,
          finalized_at: null,
          n_segments: 1,
          n_speakers: 1,
          has_minutes: false,
        },
      ]),
    });
  });
  await page.route(`**/meetings/${meetingId}/transcript`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          text: marker,
          start_ms: 0,
          end_ms: 1_000,
          speaker_id: "speaker-1",
          speaker_label: "说话人 1",
        },
      ]),
    });
  });
  await page.goto("/");

  await expect(page.getByTestId("meeting-item")).toHaveCount(1, { timeout: 5_000 });
  await expect(page.getByTestId("transcript-message")).toContainText(marker, {
    timeout: 5_000,
  });
});

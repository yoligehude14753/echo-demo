import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

const segment = {
  text: "本地转录片段",
  start_ms: 0,
  end_ms: 1_000,
  speaker_id: "speaker-1",
  speaker_label: "说话人 1",
};

async function readyStore(page: Parameters<typeof installEchoMock>[0]): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("sync-status")).toBeVisible({ timeout: 10_000 });
}

test("backend meeting.segment event enqueues a local transcript operation", async ({ page }) => {
  await installEchoMock(page);
  await readyStore(page);

  const state = await page.evaluate(async (payload) => {
    const [{ useStore }, syncState] = await Promise.all([
      import("/src/store.ts"),
      import("/src/syncState.ts"),
    ]);
    syncState.resetSyncStateForTest(window.localStorage);
    syncState.ensureSyncDeviceId();
    useStore.getState().reset();
    useStore.getState().applyEvent({
      type: "meeting.segment",
      seq: 1,
      ts: "2026-07-15T00:00:00.000Z",
      meeting_id: "meeting-event-only",
      payload,
    });
    return {
      outbox: syncState.loadSyncState().outbox,
      segments: useStore.getState().meetings["meeting-event-only"]?.segments ?? [],
    };
  }, segment);

  expect(state.outbox).toHaveLength(1);
  expect(state.outbox[0]?.entity_type).toBe("transcript_segment");
  expect(state.segments).toHaveLength(1);
});

test("chunk response followed by the same event enqueues only once", async ({ page }) => {
  await installEchoMock(page);
  await readyStore(page);

  const outbox = await page.evaluate(async (payload) => {
    const [{ useStore }, syncState] = await Promise.all([
      import("/src/store.ts"),
      import("/src/syncState.ts"),
    ]);
    syncState.resetSyncStateForTest(window.localStorage);
    syncState.ensureSyncDeviceId();
    useStore.getState().reset();
    useStore.getState().addMeetingSegments("meeting-chunk-event", [payload]);
    useStore.getState().applyEvent({
      type: "meeting.segment",
      seq: 2,
      ts: "2026-07-15T00:00:01.000Z",
      meeting_id: "meeting-chunk-event",
      payload,
    });
    return {
      count: syncState.loadSyncState().outbox.length,
      segments: useStore.getState().meetings["meeting-chunk-event"]?.segments.length ?? 0,
    };
  }, segment);

  expect(outbox).toEqual({ count: 1, segments: 1 });
});

test("remote sync apply updates the meeting without creating a local outbox item", async ({ page }) => {
  await installEchoMock(page);
  await readyStore(page);

  const state = await page.evaluate(async (payload) => {
    const [{ useStore }, syncState] = await Promise.all([
      import("/src/store.ts"),
      import("/src/syncState.ts"),
    ]);
    syncState.resetSyncStateForTest(window.localStorage);
    syncState.ensureSyncDeviceId();
    useStore.getState().reset();
    useStore.getState().applyRemoteSyncEntity("transcript_segment", {
      meeting_id: "meeting-remote",
      ...payload,
    });
    return {
      outbox: syncState.loadSyncState().outbox.length,
      segments: useStore.getState().meetings["meeting-remote"]?.segments.length ?? 0,
    };
  }, segment);

  expect(state).toEqual({ outbox: 0, segments: 1 });
});

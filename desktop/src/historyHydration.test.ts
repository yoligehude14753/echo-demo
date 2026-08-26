import assert from "node:assert/strict";
import test from "node:test";
import {
  beginMeetingDetailRequest,
  canCommitMeetingDetailRequest,
  hydrateMeetingDetailMetadata,
  resolveMeetingDetailLoadTarget,
  retryMeetingDetailMetadata,
  type MeetingDetailMetadata,
  // @ts-expect-error Node 的 strip-types runner 直接执行源码测试。
} from "./historyHydration.ts";

function emptyMetadata(): MeetingDetailMetadata {
  return {
    meetingDetailLoaded: {},
    meetingDetailErrors: {},
    meetingDetailSources: {},
    meetingDetailGenerations: {},
  };
}

test("本地缓存恢复时原子写入 loaded、source、generation 并清除旧错误", () => {
  const current = emptyMetadata();
  current.meetingDetailErrors["local-1"] = "stale";

  const hydrated = hydrateMeetingDetailMetadata(
    ["local-1", "local-2"],
    current,
    "local-cache",
  );

  assert.deepEqual(hydrated.meetingDetailLoaded, {
    "local-1": true,
    "local-2": true,
  });
  assert.deepEqual(hydrated.meetingDetailSources, {
    "local-1": "local-cache",
    "local-2": "local-cache",
  });
  assert.deepEqual(hydrated.meetingDetailGenerations, {
    "local-1": 1,
    "local-2": 1,
  });
  assert.deepEqual(hydrated.meetingDetailErrors, {});
});

test("远端 transcript 404 的迟到 catch 不能覆盖 legacy IPC 已恢复的详情", () => {
  const request = beginMeetingDetailRequest(
    "meeting-1",
    "remote",
    emptyMetadata(),
  );
  assert.equal(
    canCommitMeetingDetailRequest(request.token, request.metadata),
    true,
  );

  const locallyHydrated = hydrateMeetingDetailMetadata(
    ["meeting-1"],
    request.metadata,
    "local-legacy",
  );

  assert.equal(
    canCommitMeetingDetailRequest(request.token, locallyHydrated),
    false,
  );
  assert.equal(locallyHydrated.meetingDetailLoaded["meeting-1"], true);
  assert.equal(
    locallyHydrated.meetingDetailSources["meeting-1"],
    "local-legacy",
  );
});

test("详情错误提交 fence 分别拒绝 source、generation 与 loaded 的 stale 状态", () => {
  const request = beginMeetingDetailRequest(
    "meeting-1",
    "remote",
    emptyMetadata(),
  );
  const current = request.metadata;

  assert.equal(
    canCommitMeetingDetailRequest(request.token, {
      ...current,
      meetingDetailSources: { "meeting-1": "local-legacy" },
    }),
    false,
  );
  assert.equal(
    canCommitMeetingDetailRequest(request.token, {
      ...current,
      meetingDetailGenerations: { "meeting-1": 2 },
    }),
    false,
  );
  assert.equal(
    canCommitMeetingDetailRequest(request.token, {
      ...current,
      meetingDetailLoaded: { "meeting-1": true },
    }),
    false,
  );
});

test("远端重试即使原 loaded 为 true 也会失效旧代并安排重新拉取", () => {
  const current: MeetingDetailMetadata = {
    meetingDetailLoaded: { "remote-1": true },
    meetingDetailErrors: { "remote-1": "failed" },
    meetingDetailSources: { "remote-1": "remote" },
    meetingDetailGenerations: { "remote-1": 7 },
  };

  const retry = retryMeetingDetailMetadata("remote-1", false, current);

  assert.equal(retry.action, "reload-remote");
  assert.equal(retry.refetch, true);
  assert.equal(retry.metadata.meetingDetailLoaded["remote-1"], false);
  assert.equal(retry.metadata.meetingDetailErrors["remote-1"], undefined);
  assert.equal(retry.metadata.meetingDetailSources["remote-1"], "remote");
  assert.equal(retry.metadata.meetingDetailGenerations["remote-1"], 8);
});

test("本地已加载详情的错误被证伪时只清错误且不触发公网重试", () => {
  const current: MeetingDetailMetadata = {
    meetingDetailLoaded: { "local-1": true },
    meetingDetailErrors: { "local-1": "failed" },
    meetingDetailSources: { "local-1": "local-cache" },
    meetingDetailGenerations: { "local-1": 3 },
  };

  const retry = retryMeetingDetailMetadata("local-1", true, current);

  assert.equal(retry.action, "clear-stale-local-error");
  assert.equal(retry.refetch, false);
  assert.equal(retry.metadata.meetingDetailLoaded["local-1"], true);
  assert.equal(retry.metadata.meetingDetailErrors["local-1"], undefined);
  assert.equal(retry.metadata.meetingDetailSources["local-1"], "local-cache");
  assert.equal(retry.metadata.meetingDetailGenerations["local-1"], 3);
});

test("本地详情未加载时重试只安排 legacy IPC 数据源", () => {
  const current: MeetingDetailMetadata = {
    meetingDetailLoaded: { "local-1": false },
    meetingDetailErrors: { "local-1": "failed" },
    meetingDetailSources: { "local-1": "local-legacy" },
    meetingDetailGenerations: { "local-1": 4 },
  };

  const retry = retryMeetingDetailMetadata("local-1", true, current);

  assert.equal(retry.action, "reload-local");
  assert.equal(retry.refetch, true);
  assert.equal(retry.metadata.meetingDetailLoaded["local-1"], false);
  assert.equal(retry.metadata.meetingDetailErrors["local-1"], undefined);
  assert.equal(retry.metadata.meetingDetailSources["local-1"], "local-legacy");
  assert.equal(retry.metadata.meetingDetailGenerations["local-1"], 5);
  assert.equal(
    resolveMeetingDetailLoadTarget(
      true,
      retry.metadata.meetingDetailLoaded["local-1"] === true,
    ),
    "local-ipc",
  );
});

test("远端重试清 loaded 后的下一轮明确走 remote API", () => {
  const retry = retryMeetingDetailMetadata(
    "remote-1",
    false,
    {
      meetingDetailLoaded: { "remote-1": true },
      meetingDetailErrors: { "remote-1": "404" },
      meetingDetailSources: { "remote-1": "remote" },
      meetingDetailGenerations: { "remote-1": 2 },
    },
  );

  assert.equal(
    resolveMeetingDetailLoadTarget(
      false,
      retry.metadata.meetingDetailLoaded["remote-1"] === true,
    ),
    "remote-api",
  );
});

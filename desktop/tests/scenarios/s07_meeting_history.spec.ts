/**
 * 场景 7（P4 M_meeting_history）：左侧会议列表点击切换 → 中右面板联动
 *
 * 覆盖功能：
 *  - 启动期 GET /meetings hydrate：左侧渲染 2 条历史会议（A、B）+ 1 条"待机时段"虚拟项
 *  - 点击 meeting A → currentMeetingId 切到 A → 中间转写流显示 A 的段、右上纪要显示 A 标题、右下显示 A 的产物
 *  - 点击 meeting B → 三处面板都切到 B，没有 stale UI 残留
 *  - 点击 meeting A 后再点 meeting B（"返回时点已选中再切"场景）
 *
 * 不依赖真后端：所有 GET /meetings、/capture/recent、/meetings/{id}/transcript
 * 等都用 page.route() 在 CDP 网络层拦截。WS 推一条 artifact.ready.meeting_id=A
 * 给 A 注入产物（store 自然会存到 meetings.A.artifacts）。
 */
import { test, expect } from "@playwright/test";
import { installScenarioMock } from "./_helpers";

const T0 = "2026-05-28T01:00:00+00:00";
const T1 = "2026-05-28T02:00:00+00:00";

const MEETING_A_ID = "mtg-history-A";
const MEETING_B_ID = "mtg-history-B";

const SUMMARY_A = {
  meeting_id: MEETING_A_ID,
  title: "Q3 销售复盘",
  state: "finalized",
  started_at: T0,
  // 已有纪要的短会议也必须保留；不能被“<10s 空会议”规则误删。
  ended_at: "2026-05-28T01:00:05+00:00",
  finalized_at: "2026-05-28T01:00:06+00:00",
  n_segments: 3,
  n_speakers: 2,
  has_minutes: true,
};
const SUMMARY_B = {
  meeting_id: MEETING_B_ID,
  title: "工程同步",
  state: "ended",
  started_at: T1,
  ended_at: "2026-05-28T02:20:00+00:00",
  finalized_at: null,
  n_segments: 2,
  n_speakers: 1,
  has_minutes: false,
};

const TRANSCRIPT_A = [
  {
    text: "A-第一段：开场",
    start_ms: 0,
    end_ms: 800,
    speaker_id: "spkA1",
    speaker_label: "说话人1",
  },
  {
    text: "A-第二段：销售数据",
    start_ms: 1000,
    end_ms: 2200,
    speaker_id: "spkA2",
    speaker_label: "说话人2",
  },
  {
    text: "A-第三段：行动项",
    start_ms: 2400,
    end_ms: 3300,
    speaker_id: "spkA1",
    speaker_label: "说话人1",
  },
];

const TRANSCRIPT_B = [
  {
    text: "B-第一段：版本对齐",
    start_ms: 0,
    end_ms: 900,
    speaker_id: "spkB1",
    speaker_label: "说话人1",
  },
  {
    text: "B-第二段：风险点",
    start_ms: 1100,
    end_ms: 2200,
    speaker_id: "spkB1",
    speaker_label: "说话人1",
  },
];

const MINUTES_A = {
  meeting_id: MEETING_A_ID,
  title: "Q3 销售复盘",
  duration_sec: 1800,
  speakers: ["说话人1", "说话人2"],
  summary: "Q3 达成 95%，下季度重点拓新",
  sections: [{ heading: "亮点", bullets: ["新签 3 单", "客单价 +12%"] }],
  decisions: ["Q4 重点扩张"],
  action_items: ["李明 周五前出方案"],
  created_at: "2026-05-28T01:35:00+00:00",
};

const ARTIFACT_A = {
  artifact_id: "art-mtg-A-pptx-001",
  artifact_type: "pptx",
  title: "A 会议 PPT",
  file_path: "/tmp/art-mtg-A-pptx-001.pptx",
  mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  size_bytes: 23456,
  generation_latency_ms: 1500,
  model: "MiniMax-M2.7-mock",
  metadata: { kind: "pptx" },
};

const ARTIFACT_B = {
  artifact_id: "art-mtg-B-md-001",
  artifact_type: "markdown",
  title: "B 同步会议笔记",
  file_path: "/tmp/art-mtg-B-md-001.md",
  mime_type: "text/markdown",
  size_bytes: 1234,
  generation_latency_ms: 800,
  model: "MiniMax-M2.7-mock",
  metadata: { kind: "markdown" },
};

test("S07 · 左侧会议列表点击 A/B → 转写与纪要切换，outputs 保持全局", async ({ page }) => {
  await page.route(/\/(api\/)?meetings\/current$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "idle", meeting_id: null }),
    }),
  );
  // 1. /meetings 列表
  await page.route(/\/(api\/)?meetings(\?|$)/, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([SUMMARY_B, SUMMARY_A]),
    });
  });
  await page.route(/\/(api\/)?workflows\/runs(\?|$)/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  // 2. /meetings/{id}/transcript
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_A_ID}/transcript$`),
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(TRANSCRIPT_A),
      });
    },
  );
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_B_ID}/transcript$`),
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(TRANSCRIPT_B),
      });
    },
  );

  // 3. /meetings/{id}/minutes — A 有，B 没有
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_A_ID}/minutes$`),
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(MINUTES_A),
      });
    },
  );
  await page.route(
    new RegExp(`/(api/)?meetings/${MEETING_B_ID}/minutes$`),
    async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "minutes not generated yet" }),
      });
    },
  );

  // 4. /meetings/{id}/artifacts —— 当前后端总返回 []
  await page.route(/\/(api\/)?meetings\/[^/]+\/artifacts$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  // 5. /capture/recent —— 待机时段视图用，给个空数组让 TranscriptStream 走"等待环境音"
  await page.route(/\/(api\/)?capture\/recent/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  // 让上面的 page.route() 都生效（不被 _mock.ts 的 window.fetch 短路）
  const mock = await installScenarioMock(page, {
    skipPaths: [
      "/meetings?",
      `/meetings/${MEETING_A_ID}`,
      `/meetings/${MEETING_B_ID}`,
      "/capture/recent",
      "/workflows/runs",
    ],
  });

  await test.step("打开主界面，等连接 OK + 列表渲染 3 项（待机时段 + A + B）", async () => {
    await page.goto("/");
    await expect(page.getByTestId("pill-backend")).toBeVisible({ timeout: 5_000 });
    // 待机时段始终在
    await expect(page.getByTestId("meeting-item-ambient")).toBeVisible();
    // 两条历史会议（按 started_at DESC，B 在前）
    await expect(page.getByTestId("meeting-item")).toHaveCount(2, { timeout: 5_000 });
    await expect(
      page.locator(`[data-meeting-id="${MEETING_A_ID}"]`),
    ).toBeVisible();
    await expect(
      page.locator(`[data-meeting-id="${MEETING_B_ID}"]`),
    ).toBeVisible();
    // 首屏只请求 summary，尚未点击拉 transcript；计数必须直接来自
    // GET /meetings 的 n_segments / n_speakers，不能先显示 0。
    await expect(
      page.locator(`[data-meeting-id="${MEETING_A_ID}"]`),
    ).toContainText("3 段");
    await expect(
      page.locator(`[data-meeting-id="${MEETING_A_ID}"]`),
    ).toContainText("2 人");
    await expect(
      page.locator(`[data-meeting-id="${MEETING_B_ID}"]`),
    ).toContainText("2 段");
    await expect(
      page.locator(`[data-meeting-id="${MEETING_B_ID}"]`),
    ).toContainText("1 人");
  });

  // 通过 WS 推 artifact.ready 给两个 meeting 各注入一个产物（contains meeting_id 字段）
  // store.applyEvent 会同时写入 meetings[mid].artifacts 与全局 artifacts
  await test.step("WS 注入 A、B 各 1 个 artifact（meeting_id 关联）", async () => {
    await mock.publish({
      type: "artifact.ready",
      seq: 1,
      ts: new Date().toISOString(),
      meeting_id: MEETING_A_ID,
      payload: ARTIFACT_A,
    });
    await mock.publish({
      type: "artifact.ready",
      seq: 2,
      ts: new Date().toISOString(),
      meeting_id: MEETING_B_ID,
      payload: ARTIFACT_B,
    });
  });

  await test.step("点击会议 A → 转写与纪要切到 A，outputs 保持全局", async () => {
    await page.locator(`[data-meeting-id="${MEETING_A_ID}"]`).click();
    // 转写流切到 history 模式（meeting-history vs ambient）
    await expect(page.getByTestId("transcript-scroller")).toHaveAttribute(
      "data-mode",
      "meeting-history",
      { timeout: 5_000 },
    );
    // A 的具体段文本可见
    await expect(page.getByText("A-第一段：开场")).toBeVisible();
    await expect(page.getByText("A-第三段：行动项")).toBeVisible();
    // 不应该看到 B 的段
    await expect(page.getByText("B-第一段：版本对齐")).toHaveCount(0);

    // 纪要显示 A 标题 + summary（用 heading 角色锚定 MinutesView 内的 h2，
    // 避开 MeetingList 按钮里的同名 span 与转写流 header 的同名 span）
    await expect(
      page.getByRole("heading", { name: "Q3 销售复盘" }),
    ).toBeVisible();
    await expect(page.getByText(/Q3 达成 95%/)).toBeVisible();

    // 检查器一次只显示一个上下文；切到工作产物后仍能看到全局产物。
    await page.getByTestId("inspector-tab-artifacts").click();
    await expect(page.getByTestId("artifact-list")).toHaveAttribute(
      "data-scope",
      "global",
    );
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_A.artifact_id}"]`),
    ).toBeVisible();
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_B.artifact_id}"]`),
    ).toBeVisible();
  });

  await test.step("点击会议 B → 转写与纪要切到 B，outputs 保持全局", async () => {
    await page.locator(`[data-meeting-id="${MEETING_B_ID}"]`).click();
    await expect(page.getByTestId("transcript-scroller")).toHaveAttribute(
      "data-mode",
      "meeting-history",
    );
    await expect(page.getByText("B-第一段：版本对齐")).toBeVisible();
    await expect(page.getByText("B-第二段：风险点")).toBeVisible();
    // A 的转写文本不应残留
    await expect(page.getByText("A-第一段：开场")).toHaveCount(0);

    // B 已结束但还没拿到纪要 → 显示生成/可重试状态，不再退回空态
    await expect(page.getByTestId("minutes-generating")).toBeVisible();
    // 当前纪要主体已切到 B；A 仍可保留在历史纪要索引中。
    await expect(page.getByTestId("minutes-title")).toHaveCount(0);

    // outputs 仍为全局，A/B 两条产物都保留。
    await page.getByTestId("inspector-tab-artifacts").click();
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_B.artifact_id}"]`),
    ).toBeVisible();
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_A.artifact_id}"]`),
    ).toBeVisible();
  });

  await test.step("再切回 A → 状态仍完整（验证无 stale 缓存）", async () => {
    await page.locator(`[data-meeting-id="${MEETING_A_ID}"]`).click();
    await expect(page.getByText("A-第二段：销售数据")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Q3 销售复盘" }),
    ).toBeVisible();
    await page.getByTestId("inspector-tab-artifacts").click();
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_A.artifact_id}"]`),
    ).toBeVisible();
    await expect(page.getByText("B-第一段：版本对齐")).toHaveCount(0);
  });

  await test.step("点击「实时记录」→ 转写切回 ambient，检查器显示全局工作产物", async () => {
    await page.getByTestId("meeting-item-ambient").click();
    await expect(page.getByText("从这里开始对话")).toBeVisible();
    await expect(page.getByTestId("inspector-tab-artifacts")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // outputs 切回 global，A、B 两条都应该可见（store.artifacts 是事件流积累的）
    await expect(page.getByTestId("artifact-list")).toHaveAttribute(
      "data-scope",
      "global",
    );
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_A.artifact_id}"]`),
    ).toBeVisible();
    await expect(
      page.locator(`[data-artifact-id="${ARTIFACT_B.artifact_id}"]`),
    ).toBeVisible();
  });
});

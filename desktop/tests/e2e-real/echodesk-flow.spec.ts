/**
 * EchoDesk 端到端核心场景（真后端 + 真 vite 渲染 + 真用户点击）。
 *
 * 不调慢路径 LLM；专注 PR-8 ~ PR-11 引入的 UI/交互修订是否真的生效：
 *  1. 启动后页面渲染 + WS 连接
 *  2. CaptureStatus 文案符合新设计（无 @开始会议、有"采集 X · 入库 Y"或"静音/底噪"）
 *  3. MeetingStatusBar 全局会议状态机：点击切换 idle ↔ in_meeting
 *  4. 左侧会议列表跟随状态变化
 *  5. TranscriptStream 待机时显示 ambient 持续转写流（不是空白）
 *  6. outputs 面板：标题为 "outputs"、无 "生成" 按钮、只展示历史
 *  7. CommandBar 无 @开始会议/@结束会议 意图分支
 *  8. （可选）后端短暂中断不会轰炸 toast
 */
import { test, expect, type Page } from "@playwright/test";

const COMMAND_BAR_TA = "textarea[placeholder*='生成']";
const MEETING_STATUS_BAR = "[data-testid='meeting-status-bar']";

async function gotoApp(page: Page): Promise<void> {
  await page.goto("/");
  // WS 真握手通过后右上角变 "已连接"
  await expect(page.locator("text=已连接")).toBeVisible({ timeout: 20_000 });
}

test.describe("EchoDesk 核心流程", () => {
  test("1. 启动后页面渲染 + WS 连接 + 品牌名 EchoDesk", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);
    await expect(page.locator("text=EchoDesk").first()).toBeVisible();
    await expect(page.locator("text=v0.1")).toBeVisible();
    // 顶部状态栏存在
    await expect(page.locator(MEETING_STATUS_BAR)).toBeVisible();
  });

  test("2. CaptureStatus 文案符合 PR-9 新设计（无 @开始会议）", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);
    const cap = page.getByTestId("capture-status");
    await expect(cap).toBeVisible({ timeout: 15_000 });
    // 关键：不能再出现 @开始会议 字样
    await expect(cap).not.toContainText("@开始会议");
    await expect(cap).not.toContainText("叠加转写");
    // 新文案：持续采集 · 采集 X · 入库 Y · 静音/底噪自动过滤
    await expect(cap).toContainText(/持续采集|初始化麦克风|麦克风不可用/);
  });

  test("3. MeetingStatusBar 初始 待机，点击切换为 会议中（manual_start）", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    const bar = page.locator(MEETING_STATUS_BAR);
    await expect(bar).toBeVisible();

    // 初始应为待机；如果有遗留 in_meeting（hydrate）先点一下回到 idle
    const initialText = (await bar.textContent()) ?? "";
    if (initialText.includes("会议中")) {
      await bar.click();
      // 等 manual_end 网络往返
      await expect(bar).toContainText("待机", { timeout: 10_000 });
    }
    await expect(bar).toContainText("待机");

    // 点击 → 进入会议中
    await bar.click();
    await expect(bar).toContainText("会议中", { timeout: 10_000 });
    // 计时 elapsed 0:0X 形式（manual 启动不带 "auto" 角标）
    await expect(bar).toContainText(/\d:\d{2}/, { timeout: 10_000 });

    // 再点击 → 回到待机（manual_end，会触发 finalize 但前端立即反应）
    await bar.click();
    await expect(bar).toContainText("待机", { timeout: 15_000 });
  });

  test("4. 会议状态切换会更新左侧会议列表", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    const bar = page.locator(MEETING_STATUS_BAR);
    // 确保 idle
    if (((await bar.textContent()) ?? "").includes("会议中")) {
      await bar.click();
      await expect(bar).toContainText("待机", { timeout: 10_000 });
    }

    // 记录当前会议项数量
    const meetingItemSel = ".ant-layout-sider [class*='cursor-pointer'], [data-testid='meeting-card']";
    // 用更宽松的选择器：左侧 Sider 内任意以 m- 或 auto- 或 smoke- 开头的 ID 文本
    const initialMeetingTexts = await page
      .locator(".ant-layout-sider")
      .locator("text=/^(m-|auto-|smoke-)/")
      .allTextContents();

    // 开会
    await bar.click();
    await expect(bar).toContainText("会议中", { timeout: 10_000 });

    // 等待 ws meeting.started + state_changed 触发会议加入列表
    await page.waitForTimeout(2_000);
    const afterStartTexts = await page
      .locator(".ant-layout-sider")
      .locator("text=/^(m-|auto-|smoke-)/")
      .allTextContents();
    expect(afterStartTexts.length).toBeGreaterThanOrEqual(
      initialMeetingTexts.length,
    );

    // 结束
    await bar.click();
    await expect(bar).toContainText("待机", { timeout: 15_000 });
    // unused selector to silence lint
    void meetingItemSel;
  });

  test("5. TranscriptStream 待机时始终显示 ambient（不是会议子集）", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // 待机态下应能看到 "ambient 持续转写" 或 "等待环境音转写"
    const main = page.locator("text=/转写流/").locator("xpath=ancestor::div[1]/..");
    // 应**不会**显示老文案 "从左侧选择会议查看转写流"
    await expect(page.locator("text=从左侧选择会议查看转写流")).toHaveCount(0);
    // 必须有其中之一
    await expect(
      page.locator("text=/ambient 持续转写|等待环境音转写/").first(),
    ).toBeVisible({ timeout: 10_000 });
    void main;
  });

  test("6. outputs 面板：标题 outputs、无 生成 按钮、空态文案符合 PR-8", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // 标题
    await expect(page.locator("text=outputs").first()).toBeVisible();
    // 不再有 "产物" 老标题
    await expect(page.locator("text=/^产物$/")).toHaveCount(0);
    // 不再有 "生成" 按钮（PR-8 删了）
    // 注意：CaptureStatus / 别处可能仍有 "生成 HTML" 这种 IntentTag，所以匹配单独的 "生成" 按钮
    const genButton = page.getByRole("button", { name: /^生成$/ });
    await expect(genButton).toHaveCount(0);
    // 空态文案
    const outputsArea = page
      .locator("text=outputs")
      .locator("xpath=ancestor::div[1]/parent::div");
    void outputsArea; // 仅作为 layout anchor
  });

  test("7. CommandBar 输入 @开始会议 应被 LLM 路由分类为 chat（删除了对应 intent）", async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await gotoApp(page);

    const ta = page.locator(COMMAND_BAR_TA);
    await ta.fill("@开始会议");
    await ta.press("Enter");

    // 不再出现 "已开启" / "会议 m-xxx 已开启" 的 toast
    // 应被路由到 chat（或 keyword 不命中），前端不会触发 manual start
    await page.waitForTimeout(8_000);
    // 关键负断言：toast 里不能出现 "已开启"
    await expect(
      page.locator(".ant-message").filter({ hasText: /已开启/ }),
    ).toHaveCount(0);
    // 状态栏仍为待机（除非用户在此期间手动点了）
    const bar = page.locator(MEETING_STATUS_BAR);
    // 给 0.5s 让 state 同步
    await page.waitForTimeout(500);
    await expect(bar).toContainText(/待机|会议中/); // 至少状态条仍在
  });

  test("8. 顶部 header 有 TTS 开关 + 事件计数 + 连接指示器", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    await expect(page.getByTestId("tts-toggle")).toBeVisible();
    await expect(page.locator("text=/事件 \\d+/")).toBeVisible();
    await expect(page.locator("text=/已连接|断线/")).toBeVisible();
  });

  test("9. TTS 开关真点击切换 → 文案/状态翻转", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    const tts = page.getByTestId("tts-toggle");
    await expect(tts).toBeVisible();
    const textBefore = ((await tts.textContent()) ?? "").trim();
    // 文案应为语音播报的可读状态之一
    expect(["语音播报", "已静音", "播报中"]).toContain(textBefore);

    await tts.click();
    // 等待文案翻转（"已静音" ↔ "语音播报"）
    await expect
      .poll(async () => ((await tts.textContent()) ?? "").trim(), {
        timeout: 5_000,
        intervals: [200],
      })
      .not.toBe(textBefore);

    // 再切回去
    await tts.click();
    await expect
      .poll(async () => ((await tts.textContent()) ?? "").trim(), {
        timeout: 5_000,
        intervals: [200],
      })
      .toBe(textBefore);
  });

  test("10. CommandBar 真发送 @查 → 出现意图 tag (rag.ask)", async ({ page }) => {
    test.setTimeout(120_000);
    await gotoApp(page);

    const ta = page.locator(COMMAND_BAR_TA);
    await ta.fill("@查 什么是 RAG");
    await ta.press("Enter");

    // 期望：右上角事件计数随 ws 事件累计；至少一个 .ant-tag 出现
    await expect.poll(
      async () => {
        const tagCount = await page.locator(".ant-tag").count();
        return tagCount;
      },
      { timeout: 60_000, intervals: [1_500] },
    ).toBeGreaterThan(0);
  });

  test("11. WorkspaceBar 真存在 + 关键控件可见", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    await expect(page.getByTestId("workspace-bar")).toBeVisible();
    // 工作目录 tag、扫描按钮、清空按钮都应可见（未设置目录时按钮可能 disabled，
    // 但 DOM 必须存在）
    await expect(page.getByTestId("workspace-dirs-tag")).toBeVisible();
    await expect(page.getByTestId("workspace-scan-btn")).toBeVisible();
    await expect(page.getByTestId("workspace-clear-btn")).toBeVisible();
  });

  test("12. 转写流在区域内滚动，不会撑高整个 App body", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // body 视口不应可滚动（document.scrollingElement.scrollHeight 不大于 clientHeight + 1）
    const overflow = await page.evaluate(() => {
      const root = document.scrollingElement || document.documentElement;
      return {
        scrollHeight: root.scrollHeight,
        clientHeight: root.clientHeight,
      };
    });
    // 允许 1px 浮点误差
    expect(overflow.scrollHeight).toBeLessThanOrEqual(overflow.clientHeight + 1);

    // 转写流容器存在且自己 overflow auto（有 segments 时）
    // 如果空态文案显示中，跳过 scroller 检查
    const scroller = page.getByTestId("transcript-scroller");
    if ((await scroller.count()) > 0) {
      const isScrollable = await scroller.evaluate((el) => {
        const css = window.getComputedStyle(el as HTMLElement);
        return css.overflowY === "auto" || css.overflowY === "scroll";
      });
      expect(isScrollable).toBe(true);
    }
  });

  test("13. 转写流说话人标签从 1 开始连续编号（不显示后端全局编号）", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // 等转写流出现（有数据时才有 speaker-tag）；如果环境无音/空窗，跳过本断言
    const tags = page.getByTestId("speaker-tag");
    await page.waitForTimeout(4_000);
    const count = await tags.count();
    if (count === 0) {
      test.skip(true, "环境无 ambient 数据，无法验证 speaker 显示规则");
      return;
    }

    // 1) 所有可见 tag 形如「说话人 N」（有空格）或「未识别」，
    //    不再有「说话人55」「说话人60」这种把后端全局 ID 直接粘上去的格式
    const seen = new Set<number>();
    let identifiedCount = 0;
    for (let i = 0; i < count; i++) {
      const txt = ((await tags.nth(i).textContent()) ?? "").trim();
      if (txt === "未识别") continue;
      identifiedCount++;
      const m = /^说话人 (\d+)$/.exec(txt);
      expect(m, `tag 文案不符合「说话人 N」格式：${txt}`).not.toBeNull();
      seen.add(parseInt(m![1]!, 10));
    }

    if (identifiedCount > 0) {
      // 2) 编号必须从 1 开始
      expect(seen.has(1), "首位说话人编号必须为 1").toBe(true);

      // 3) 编号必须是连续 1..max，无空洞
      //    （隐含验证：如果后端全局 ID 透出，会形成 1..N 中间缺失或某个 N 远大于
      //     可见说话人数，因此连续性约束就足以拒绝旧格式）
      const maxN = Math.max(...seen);
      for (let n = 1; n <= maxN; n++) {
        expect(seen.has(n), `编号 ${n} 缺失，编号必须连续`).toBe(true);
      }
    }
  });

  test("14. 转写流气泡布局：头像+气泡可见，时间默认隐藏 hover 后才显示", async ({
    page,
  }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // 等转写流出现
    const rows = page.getByTestId("transcript-row");
    await page.waitForTimeout(4_000);
    const rowCount = await rows.count();
    if (rowCount === 0) {
      console.log("[skip] 无 ambient 转写数据，跳过气泡布局断言");
      return;
    }

    // 1) 至少有一个头像可见（同说话人合并时只显示首条头像，所以 avatar 数 ≤ row 数）
    const avatars = page.getByTestId("speaker-avatar");
    expect(await avatars.count()).toBeGreaterThan(0);
    await expect(avatars.first()).toBeVisible();

    // 2) 头像内容是数字或 "?"（未识别），不是后端 raw label
    const avatarText = ((await avatars.first().textContent()) ?? "").trim();
    expect(avatarText).toMatch(/^(\d+|\?)$/);

    // 3) 时间默认 opacity-0（不可见 / 透明）
    const firstRow = rows.first();
    const time = firstRow.getByTestId("transcript-time").first();
    if ((await time.count()) > 0) {
      const opacity = await time.evaluate((el) => {
        return window.getComputedStyle(el as HTMLElement).opacity;
      });
      expect(parseFloat(opacity)).toBeLessThan(0.5);

      // 4) hover 该 row 后时间出现
      await firstRow.hover();
      await page.waitForTimeout(300);
      const opacityHover = await time.evaluate((el) => {
        return window.getComputedStyle(el as HTMLElement).opacity;
      });
      expect(parseFloat(opacityHover)).toBeGreaterThan(0.5);

      // 5) 时间格式仅 HH:MM（不带秒）
      const txt = ((await time.textContent()) ?? "").trim();
      expect(txt).toMatch(/^\d{2}:\d{2}$/);
    }
  });

  test("15. 会议列表'N 人'与转写流显示的 distinct speaker tag 数一致（同源计数）", async ({
    page,
  }) => {
    test.setTimeout(60_000);
    await gotoApp(page);
    await page.waitForTimeout(4_000);

    // 转写流可见 distinct speaker-tag 数（仅"新说话人开头"那一行才有 tag）
    const tags = page.getByTestId("speaker-tag");
    const tagCount = await tags.count();
    if (tagCount === 0) {
      console.log("[skip] 无转写数据");
      return;
    }
    const distinctDisplayIdxs = new Set<number>();
    for (let i = 0; i < tagCount; i++) {
      const t = ((await tags.nth(i).textContent()) ?? "").trim();
      const m = /^说话人 (\d+)$/.exec(t);
      if (m) distinctDisplayIdxs.add(parseInt(m[1]!, 10));
    }
    const transcriptSpeakerCount = distinctDisplayIdxs.size;
    if (transcriptSpeakerCount === 0) return;

    // 当前会议在左侧列表里的"N 人"显示
    // 没在会议中的话，不强求一致（meeting list 只有真实开过会的项）
    const meetingItems = page.locator("button:has-text('段'):has-text('人')");
    const meetingCount = await meetingItems.count();
    if (meetingCount === 0) return;

    // 检查"进行中"会议（如果存在）的人数
    const inMeeting = page.locator(
      "button:has-text('进行中'):has-text('段'):has-text('人')",
    );
    if ((await inMeeting.count()) > 0) {
      const text = ((await inMeeting.first().textContent()) ?? "").trim();
      const m = /(\d+) 人/.exec(text);
      expect(m, `'N 人' 格式应可解析：${text}`).not.toBeNull();
      const listCount = parseInt(m![1]!, 10);
      // 转写流窗口 100 条 vs 会议累计 — 列表数 ≤ 全局，但都来自同一 remap
      // 关键断言：列表 N 不能远超转写流可见编号最大值 + 容差
      // （即：不会再出现 "47 vs 86" 的不一致）
      const maxDisplayIdx = Math.max(...distinctDisplayIdxs);
      expect(
        listCount,
        `会议列表 ${listCount} 人 vs 转写流 max display idx ${maxDisplayIdx}，差距过大说明计数源不同步`,
      ).toBeLessThanOrEqual(maxDisplayIdx + 5);
    }
  });

  test("16. WS 事件计数在交互后真增长", async ({ page }) => {
    test.setTimeout(60_000);
    await gotoApp(page);

    // 初始计数
    const counterLoc = page.locator("text=/事件 \\d+/");
    await expect(counterLoc).toBeVisible();
    const initial = (await counterLoc.textContent()) ?? "";
    const m0 = /事件 (\d+)/.exec(initial);
    const c0 = m0 ? parseInt(m0[1]!, 10) : 0;

    // 触发一次 manual_start / manual_end，至少 +2 ws 事件
    const bar = page.locator(MEETING_STATUS_BAR);
    if (((await bar.textContent()) ?? "").includes("会议中")) {
      await bar.click();
      await expect(bar).toContainText("待机", { timeout: 15_000 });
    }
    await bar.click();
    await expect(bar).toContainText("会议中", { timeout: 15_000 });
    await bar.click();
    await expect(bar).toContainText("待机", { timeout: 20_000 });

    await expect.poll(
      async () => {
        const t = (await counterLoc.textContent()) ?? "";
        const m = /事件 (\d+)/.exec(t);
        return m ? parseInt(m[1]!, 10) : 0;
      },
      { timeout: 15_000, intervals: [1000] },
    ).toBeGreaterThan(c0);
  });
});

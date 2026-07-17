/**
 * E2E：first-run 引导流程（P3.1）。
 *
 * - 清掉 localStorage 的 onboarding.completed 标志
 * - mock /admin/data-dir，模拟非 Electron 环境下 navigator.permissions
 * - 验证：3 步引导可前进 + 跳过会立即关闭 + 完成会写持久化（重启不再弹）
 * - 验证：完成后从设置重新打开，必须从欢迎页重新开始
 */
import { test, expect } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("first-run 引导：3 步可前进 + 完成后持久化", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __getUserMediaCalls?: number }).__getUserMediaCalls = 0;
    const mediaDevices = navigator.mediaDevices ?? ({} as MediaDevices);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        ...mediaDevices,
        enumerateDevices: async () => [],
        getUserMedia: async () => {
          (window as unknown as { __getUserMediaCalls: number }).__getUserMediaCalls += 1;
          return new MediaStream();
        },
      },
    });
  });
  // 拦截 /admin/data-dir：让欢迎步骤显示真实路径
  await page.route(/\/admin\/data-dir$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        path: "/Users/test/.echodesk",
        exists: true,
        size_bytes: 0,
        breakdown: { db: 0, storage: 0, rag_index: 0, logs: 0, skill_build: 0 },
      }),
    });
  });

  await installEchoMock(page, { keepOnboarding: true });
  await page.goto("/");

  // 引导 Modal 应自动弹出
  await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
  await expect(page.getByTestId("onboarding-local-runtime-copy")).toContainText(
    "都跑在你自己的电脑上",
  );
  await expect(page.getByText("/Users/test/.echodesk")).toBeVisible();
  await expect(page.getByTestId("onboarding-native-runtime-contract")).toHaveCount(0);
  expect(
    await page.evaluate(
      () => (window as unknown as { __getUserMediaCalls?: number }).__getUserMediaCalls ?? 0,
    ),
  ).toBe(0);

  // 第一步 → 第二步（麦克风）
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByText("授权麦克风")).toBeVisible();

  // 第二步 → 第三步（完成）
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByText("准备就绪")).toBeVisible();

  // 完成关闭
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByText("准备就绪")).not.toBeVisible({ timeout: 3_000 });
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as unknown as { __getUserMediaCalls?: number }).__getUserMediaCalls ?? 0,
      ),
    )
    .toBe(1);

  // 持久化校验：reload 后引导不再弹
  await page.reload();
  await page.waitForTimeout(500); // 给一帧时间，可能弹也可能不弹
  await expect(page.getByText("欢迎来到 EchoDesk")).not.toBeVisible({ timeout: 1_000 });

  // 回归：外层 OnboardingModal 不会随 AntD Modal 内容销毁，过去会保留在第 3 步。
  // 从设置重新打开必须回到第 1 步，并恢复“下一步”而不是“开始使用”。
  await page.getByTestId("open-settings").click();
  await page.getByTestId("replay-onboarding").click();
  await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText("准备就绪")).not.toBeVisible();
  await expect(page.getByTestId("onboarding-prev")).toHaveCount(0);
  await expect(page.getByTestId("onboarding-next")).toHaveText("下一步");
});

test("first-run 引导：跳过按钮立即关闭", async ({ page }) => {
  await installEchoMock(page, { keepOnboarding: true });
  await page.goto("/");

  await expect(page.getByText("欢迎来到 EchoDesk")).toBeVisible({ timeout: 5_000 });
  await page.getByTestId("onboarding-skip").click();
  await expect(page.getByText("欢迎来到 EchoDesk")).not.toBeVisible({ timeout: 3_000 });
});

test("native Android 引导明确 remote-mobile、public endpoint 与 paired Hub 边界", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");
  await page.evaluate(() => {
    (
      window as unknown as {
        Capacitor?: { isNativePlatform: () => boolean };
      }
    ).Capacitor = { isNativePlatform: () => true };
  });
  await page.getByTestId("open-settings").click();
  await page.getByTestId("replay-onboarding").click();

  const contract = page.getByTestId("onboarding-native-runtime-contract");
  await expect(contract).toBeVisible({ timeout: 5_000 });
  await expect(page.getByTestId("onboarding-native-runtime-copy")).toContainText(
    "远程移动端",
  );
  await expect(page.getByTestId("onboarding-native-runtime-copy")).toContainText(
    "不会启动桌面 backend 或 bundled worker",
  );
  await expect(contract).toContainText("https://echodesk.yoliyoli.uk");
  await expect(contract).toContainText("固定连接该公共服务");
  await expect(contract).toContainText("不能在设置中改写业务 endpoint");
  await expect(contract).toContainText("Hub 地址并使用配对码");
  await expect(page.getByTestId("onboarding-local-runtime-copy")).toHaveCount(0);
  await expect(page.getByText("~/.echodesk/", { exact: true })).toHaveCount(0);
  await expect(page.getByText("数据存放位置", { exact: true })).toHaveCount(0);

  await page.getByTestId("onboarding-next").click();
  await expect(
    page.getByText("本 Preview 固定的公共服务 https://echodesk.yoliyoli.uk"),
  ).toBeVisible();
  await expect(page.getByText("手机内没有本地 ASR 或桌面 bundled backend")).toBeVisible();
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByTestId("onboarding-native-done-copy")).toContainText(
    "单端或多端收音",
  );
  await expect(page.getByTestId("onboarding-native-done-copy")).toContainText(
    "转写与纪要依赖远程服务可用",
  );
  await expect(page.getByTestId("onboarding-native-done-copy")).toContainText(
    "多端同步需要另行配对 Hub",
  );
});

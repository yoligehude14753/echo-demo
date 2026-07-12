import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("settings restart stays busy and cannot dispatch duplicate IPC calls", async ({ page }) => {
  await installEchoMock(page, { skipPaths: ["/admin/settings/remote"] });
  await page.route(/\/(api\/)?admin\/settings\/remote$/, (route) => {
    if (route.request().method() === "PATCH") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          written_keys: ["llm_main_base_url"],
          restart_required: true,
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        config_path: "/tmp/echodesk-user.json",
        fields: [
          {
            key: "llm_main_base_url",
            value: "",
            sensitive: false,
            source: "default",
          },
        ],
      }),
    });
  });
  await page.goto("/");
  await page.evaluate(() => {
    const state = window as unknown as {
      echo?: Record<string, unknown>;
      __restartCalls?: number;
      __finishRestart?: () => void;
    };
    state.__restartCalls = 0;
    state.echo = state.echo ?? {};
    state.echo.manualRestartBackend = () =>
      new Promise((resolve) => {
        state.__restartCalls = (state.__restartCalls ?? 0) + 1;
        state.__finishRestart = () => resolve({ ok: true, generation: 1 });
      });
  });

  await page.getByTestId("open-settings").click();
  const baseUrl = page.getByRole("textbox", { name: /^主 LLM Base URL/ });
  await baseUrl.fill("https://model.example.test/v1");
  await page.getByTestId("save-remote-settings").click();
  const restart = page.getByTestId("restart-backend-after-config");
  await expect(restart).toBeVisible();
  await restart.click();
  await expect(restart).toBeDisabled();
  await expect(restart).toContainText("正在重启服务");
  await restart.click({ force: true });
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as unknown as { __restartCalls?: number }).__restartCalls ?? 0,
      ),
    )
    .toBe(1);

  await page.evaluate(() =>
    (window as unknown as { __finishRestart?: () => void }).__finishRestart?.(),
  );
  await expect(page.getByText("服务重启已开始")).toBeVisible();
  await expect(restart).toHaveCount(0);
});

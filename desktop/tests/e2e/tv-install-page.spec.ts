import { expect, test } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";

const currentDir = dirname(fileURLToPath(import.meta.url));

test("电视安装页：大屏链接和遥控器确认键路径可用", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });

  const installPage = pathToFileURL(
    resolve(currentDir, "../../../docs/tv-install.html"),
  ).toString();
  await page.goto(installPage);

  await expect(page.getByRole("heading", { name: "EchoDesk 智能电视安装" })).toBeVisible();

  const apkLink = page.getByTestId("tv-apk-link");
  await expect(apkLink).toBeVisible();
  await expect(apkLink).toHaveAttribute(
    "href",
    "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.4/EchoDesk-0.2.4-smart-tv.apk",
  );
  await apkLink.focus();
  await expect(apkLink).toBeFocused();

  const bundleLink = page.getByTestId("tv-bundle-link");
  await expect(bundleLink).toHaveAttribute(
    "href",
    "https://github.com/yoligehude14753/echo-demo/releases/download/v0.2.4/EchoDesk-0.2.4-smart-tv-oneclick.zip",
  );
  await bundleLink.focus();
  await expect(bundleLink).toBeFocused();

  const copyButton = page.getByTestId("copy-adb-command");
  await copyButton.focus();
  await expect(copyButton).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("copy-status")).toContainText("./install-tv-macos.sh");
});

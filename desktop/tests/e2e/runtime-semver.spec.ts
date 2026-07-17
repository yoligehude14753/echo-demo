import { expect, test } from "@playwright/test";

test("renderer 按 SemVer 优先级区分预发布版与正式版", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const comparisons = await page.evaluate(async () => {
    const { compareVersions } = await import("/src/runtime.ts");
    return {
      prereleaseBeforeRelease: compareVersions("0.3.1-rc.1", "0.3.1"),
      releaseAfterPrerelease: compareVersions("0.3.1", "0.3.1-rc.1"),
      numericPrereleaseOrder: compareVersions("0.3.1-rc.10", "0.3.1-rc.2"),
      lexicalPrereleaseOrder: compareVersions("0.3.1-beta", "0.3.1-alpha"),
      buildMetadataIgnored: compareVersions("v0.3.1+mac.1", "0.3.1+win.2"),
    };
  });

  expect(comparisons).toEqual({
    prereleaseBeforeRelease: -1,
    releaseAfterPrerelease: 1,
    numericPrereleaseOrder: 1,
    lexicalPrereleaseOrder: 1,
    buildMetadataIgnored: 0,
  });
});

test("renderer 更新选择忽略 adhoc、跨频道和缺当前平台资产的 release", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const selected = await page.evaluate(async () => {
    (window as unknown as { Capacitor: { isNativePlatform: () => boolean } }).Capacitor = {
      isNativePlatform: () => true,
    };
    const { selectCompatibleAppUpdate } = await import("/src/runtime.ts");
    const digest = `sha256:${"a".repeat(64)}`;
    const makeRelease = (
      tag: string,
      prerelease: boolean,
      assetName: string,
    ) => ({
      tag_name: tag,
      name: tag,
      html_url: `https://example.invalid/${tag}`,
      prerelease,
      draft: false,
      assets: [
        {
          name: assetName,
          size: 123,
          digest,
          browser_download_url: `https://github.com/example/${assetName}`,
        },
      ],
    });
    const release = selectCompatibleAppUpdate(
      [
        makeRelease("vadhoc-test", true, "EchoDesk-adhoc.apk"),
        makeRelease(
          "v0.3.4",
          false,
          "EchoDesk-0.3.4-android-universal-PREVIEW.apk",
        ),
        makeRelease(
          "v0.3.4-preview.1",
          true,
          "EchoDesk-0.3.4-preview.1-android-universal-PREVIEW.apk",
        ),
        {
          ...makeRelease(
            "v0.3.3-preview.6",
            true,
            "EchoDesk-0.3.3-preview.6-android-universal-PREVIEW.apk",
          ),
          assets: [{
            name: "EchoDesk-0.3.3-preview.6-android-universal-PREVIEW.apk",
            size: 123,
            browser_download_url: "https://github.com/example/no-digest.apk",
          }],
        },
        makeRelease("v0.3.3-preview.5", true, "EchoDesk-unsupported.apk"),
        makeRelease(
          "v0.3.3-preview.4",
          true,
          "EchoDesk-0.3.3-preview.4-android-universal-PREVIEW.apk",
        ),
      ],
      "0.3.3-preview.3",
      "preview",
    );
    return release && { version: release.version, asset: release.asset.name };
  });
  expect(selected).toEqual({
    version: "0.3.3-preview.4",
    asset: "EchoDesk-0.3.3-preview.4-android-universal-PREVIEW.apk",
  });
});

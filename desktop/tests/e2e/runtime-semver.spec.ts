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

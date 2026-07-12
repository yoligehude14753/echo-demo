import { expect, test, type Page } from "@playwright/test";
import { installEchoMock } from "./_mock";

const ORIGIN_A = "https://settings-a.example";
const ORIGIN_B = "https://settings-b.example";
const ORIGIN_C = "https://settings-c.example";
const HYDRATE_PATHS = [
  "/admin/data-dir",
  "/admin/settings/remote",
  "/healthz/full",
  "/workspace/status",
] as const;

interface SettingsFenceState {
  pending: Array<{
    path: string;
    resolve(response: Response): void;
    response: Response;
  }>;
  completed: number;
  mutations: Array<{ method: string; origin: string; path: string }>;
  releaseA(): number;
}

async function openSettingsHarness(page: Page, deferA: boolean): Promise<void> {
  await page.addInitScript((origin) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", origin);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
  }, ORIGIN_A);
  await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.evaluate(
    ({ originA, originB, originC, hydratePaths, shouldDeferA }) => {
      const originalFetch = window.fetch.bind(window);
      const origins = new Set([originA, originB, originC]);
      const hydrate = new Set<string>(hydratePaths);
      const json = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      const labelFor = (origin: string): string =>
        new URL(origin).hostname.replace(/^settings-/, "").replace(/\.example$/, "");
      const fixture = (origin: string, path: string): Response => {
        const label = labelFor(origin);
        if (path === "/admin/data-dir") {
          return json({
            path: `/${label}-data`,
            exists: true,
            size_bytes: 4096,
            breakdown: {
              db: 1024,
              storage: 1024,
              rag_index: 1024,
              logs: 1024,
              skill_build: 0,
            },
          });
        }
        if (path === "/admin/settings/remote") {
          return json({
            config_path: `/${label}-config.json`,
            fields: [
              {
                key: "llm_main_base_url",
                value: `https://model-${label}.example/v1`,
                sensitive: false,
                source: "user",
              },
            ],
          });
        }
        if (path === "/healthz/full") {
          return json({ backend: { ok: true, version: `0.3.1-${label}` } });
        }
        return json({
          configured_dirs: [`/workspace-${label}`],
          authorized_dirs: [`/workspace-${label}`],
          n_indexed: label.charCodeAt(0),
          max_file_mb: 100,
          scan_on_startup: true,
        });
      };
      const state: SettingsFenceState = {
        pending: [],
        completed: 0,
        mutations: [],
        releaseA: () => 0,
      };
      state.releaseA = () => {
        const pending = state.pending.splice(0);
        for (const request of pending) request.resolve(request.response);
        return pending.length;
      };
      (
        window as unknown as { __settingsOriginFence__: SettingsFenceState }
      ).__settingsOriginFence__ = state;

      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const raw =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const url = new URL(raw, window.location.href);
        const method = (init?.method ?? "GET").toUpperCase();
        const path = url.pathname.replace(/^\/api(?=\/)/, "");
        if (!origins.has(url.origin)) return originalFetch(input, init);

        if (method === "GET" && hydrate.has(path)) {
          const response = fixture(url.origin, path);
          if (shouldDeferA && url.origin === originA) {
            const deferred = await new Promise<Response>((resolve) => {
              state.pending.push({ path, resolve, response });
            });
            state.completed += 1;
            return deferred;
          }
          return response;
        }

        const mutationPaths = new Set([
          "/admin/settings/remote",
          "/admin/speakers/reset",
          "/workspace/remove-dir",
          "/workspace/scan",
        ]);
        if (method !== "GET" && mutationPaths.has(path)) {
          state.mutations.push({ method, origin: url.origin, path });
          if (path === "/admin/settings/remote") {
            return json({ written_keys: ["llm_main_base_url"], restart_required: false });
          }
          if (path === "/admin/speakers/reset") {
            return json({ speakers_deleted: 1, segments_cleared: 2 });
          }
          if (path === "/workspace/remove-dir") {
            return json({ removed: true, path: "/workspace-a", configured_dirs: [] });
          }
          return json({
            n_total: 1,
            n_added: 0,
            n_updated: 0,
            n_removed: 0,
            n_skipped: 1,
            n_failed: 0,
            duration_s: 0.01,
            errors: [],
          });
        }
        return originalFetch(input, init);
      };
    },
    {
      originA: ORIGIN_A,
      originB: ORIGIN_B,
      originC: ORIGIN_C,
      hydratePaths: HYDRATE_PATHS,
      shouldDeferA: deferA,
    },
  );

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("settings-drawer")).toBeVisible();
}

async function switchOrigin(page: Page, origin: string): Promise<void> {
  await page.evaluate(async (nextOrigin) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(nextOrigin);
  }, origin);
}

async function expectOriginState(page: Page, label: string): Promise<void> {
  await expect(page.getByText(`/${label}-data`, { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /^主 LLM Base URL/ })).toHaveValue(
    `https://model-${label}.example/v1`,
  );
  await expect(page.getByTestId("workspace-dir-row")).toContainText(
    `/workspace-${label}`,
  );
  await expect(page.getByTestId("settings-backend-version")).toContainText(
    `v0.3.1-${label}`,
  );
}

test("same-class private origin switch rejects stale Settings hydrate", async ({ page }) => {
  await openSettingsHarness(page, true);
  await expect
    .poll(() =>
      page.evaluate(() =>
        Array.from(
          new Set(
            (
              window as unknown as { __settingsOriginFence__: SettingsFenceState }
            ).__settingsOriginFence__.pending.map((request) => request.path),
          ),
        ).sort(),
      ),
    )
    .toEqual([...HYDRATE_PATHS].sort());

  await switchOrigin(page, ORIGIN_B);
  await expectOriginState(page, "b");

  const released = await page.evaluate(() =>
    (
      window as unknown as { __settingsOriginFence__: SettingsFenceState }
    ).__settingsOriginFence__.releaseA(),
  );
  expect(released).toBeGreaterThanOrEqual(HYDRATE_PATHS.length);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as { __settingsOriginFence__: SettingsFenceState }
          ).__settingsOriginFence__.completed,
      ),
    )
    .toBe(released);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve())),
      ),
  );

  await expectOriginState(page, "b");
  await expect(page.getByText("/a-data", { exact: true })).toHaveCount(0);
  await expect(page.getByText("/workspace-a", { exact: true })).toHaveCount(0);
});

test("stale Settings form and confirmations cannot mutate the next origin", async ({
  page,
}) => {
  await openSettingsHarness(page, false);
  await expectOriginState(page, "a");
  await page
    .getByRole("textbox", { name: /^主 LLM Base URL/ })
    .fill("https://stale-a.example/v1");
  await page.getByRole("button", { name: "移除 /workspace-a" }).click();
  const removeConfirmTitle = page.locator(".ant-modal-confirm-title", {
    hasText: "移除工作区目录？",
  });
  await expect(removeConfirmTitle).toBeVisible();

  await page.evaluate(async (nextOrigin) => {
    const save = document.querySelector<HTMLButtonElement>(
      '[data-testid="save-remote-settings"]',
    );
    const scan = document.querySelector<HTMLButtonElement>(
      '[data-testid="workspace-rescan"]',
    );
    const remove = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".ant-modal-confirm button"),
    ).find((button) => button.textContent?.replace(/\s/g, "") === "移除");
    if (!save || !scan || !remove) throw new Error("missing stale Settings controls");
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(nextOrigin);
    save.click();
    scan.click();
    remove.click();
  }, ORIGIN_B);

  await expectOriginState(page, "b");
  await expect(removeConfirmTitle).toHaveCount(0);
  await page.waitForTimeout(250);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as { __settingsOriginFence__: SettingsFenceState }
          ).__settingsOriginFence__.mutations,
      ),
    )
    .toEqual([]);

  await page.getByTestId("mobile-backend-base").fill("http://192.168.8.20:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  await expect(
    page.getByRole("dialog", { name: "确认使用局域网明文连接？" }),
  ).toBeVisible();
  await page.evaluate(async (nextOrigin) => {
    const confirm = Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'),
    ).find(
      (button) =>
        button.textContent?.replace(/\s/g, "") === "确认仅用于可信局域网",
    );
    if (!confirm) throw new Error("missing private backend confirmation");
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(nextOrigin);
    confirm.click();
  }, ORIGIN_C);

  await expectOriginState(page, "c");
  await expect(
    page.getByRole("dialog", { name: "确认使用局域网明文连接？" }),
  ).toHaveCount(0);
  await expect(page.getByTestId("mobile-backend-base")).toHaveValue(ORIGIN_C);
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBe(ORIGIN_C);
});

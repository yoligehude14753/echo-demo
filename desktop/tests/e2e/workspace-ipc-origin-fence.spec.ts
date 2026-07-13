import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

const ORIGIN_A = "https://workspace-a.example";
const ORIGIN_B = "https://workspace-b.example";

interface WorkspaceIpcFenceState {
  statusOrigins: string[];
  scanOrigins: string[];
  cancelledOrigins: string[];
  scanPending: boolean;
}

test("public Electron workspace scan is cancelled on A to B origin switch", async ({
  page,
}) => {
  await page.addInitScript(({ originA }) => {
    window.localStorage.setItem("echodesk.mobileBackendBase", originA);
    window.localStorage.setItem("echodesk.mobileBackendBase.userSet", "1");
    window.localStorage.setItem(
      "echodesk.publicDataBoundary.v2",
      JSON.stringify({ schema: 3, appVersion: "0.3.1" }),
    );

    const state: WorkspaceIpcFenceState = {
      statusOrigins: [],
      scanOrigins: [],
      cancelledOrigins: [],
      scanPending: false,
    };
    let rejectScan: ((reason?: unknown) => void) | null = null;
    (
      window as unknown as { __workspaceIpcFence__: WorkspaceIpcFenceState }
    ).__workspaceIpcFence__ = state;
    (window as unknown as { echo?: Record<string, unknown> }).echo = {
      isElectron: true,
      isPublicDemo: true,
      backendHost: originA,
      getLocalWorkspaceStatus: async (context: {
        expectedBackendOrigin: string;
      }) => {
        state.statusOrigins.push(context.expectedBackendOrigin);
        return {
          configured_dirs: ["/workspace-a"],
          authorized_dirs: ["/workspace-a"],
          n_indexed: 1,
          max_file_mb: 100,
          scan_on_startup: false,
        };
      },
      scanLocalWorkspaces: async (context: {
        expectedBackendOrigin: string;
      }) => {
        state.scanOrigins.push(context.expectedBackendOrigin);
        state.scanPending = true;
        return new Promise((_resolve, reject) => {
          rejectScan = reject;
        });
      },
      clearLocalWorkspaceDocs: async () => ({ n_removed: 0 }),
      addLocalWorkspaceDir: async () => ({
        added: false,
        path: "/workspace-a",
        configured_dirs: ["/workspace-a"],
      }),
      removeLocalWorkspaceDir: async () => ({
        removed: false,
        path: "/workspace-a",
        configured_dirs: ["/workspace-a"],
      }),
      cancelLocalWorkspaceOperations: async (context: {
        expectedBackendOrigin: string;
      }) => {
        state.cancelledOrigins.push(context.expectedBackendOrigin);
        state.scanPending = false;
        rejectScan?.(new DOMException("backend origin changed", "AbortError"));
        rejectScan = null;
        return { cancelled: 1 };
      },
    };
  }, { originA: ORIGIN_A });

  const mock = await installEchoMock(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("workspace-dir-row")).toContainText("/workspace-a");

  await page.getByTestId("workspace-rescan").click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __workspaceIpcFence__: WorkspaceIpcFenceState;
            }
          ).__workspaceIpcFence__.scanPending,
      ),
    )
    .toBe(true);

  await page.evaluate(async (originB) => {
    const runtime = await import("/src/runtime.ts");
    runtime.setStoredBackendBase(originB);
  }, ORIGIN_B);

  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              __workspaceIpcFence__: WorkspaceIpcFenceState;
            }
          ).__workspaceIpcFence__.cancelledOrigins,
      ),
    )
    .toEqual([ORIGIN_A]);

  const state = await page.evaluate(
    () =>
      (
        window as unknown as {
          __workspaceIpcFence__: WorkspaceIpcFenceState;
        }
      ).__workspaceIpcFence__,
  );
  expect(state.scanOrigins).toEqual([ORIGIN_A]);
  expect(state.statusOrigins.every((origin) => origin === ORIGIN_A)).toBe(true);
  expect(state.scanPending).toBe(false);
  await expect(page.getByText("扫描完成：", { exact: false })).toHaveCount(0);

  const workspaceFetches = (await mock.fetchLog()).filter((entry) =>
    /\/(?:api\/)?workspace\//.test(new URL(entry.url, page.url()).pathname),
  );
  expect(workspaceFetches).toEqual([]);
});

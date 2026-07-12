import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("工作区弹窗展示知识库文档列表并可删除单条文档", async ({ page }) => {
  let deletedDocId: string | null = null;
  const docs = [
    {
      doc_id: "doc-ws-1",
      title: "项目需求.md",
      kind: "md",
      source: "workspace",
      source_path: "/Users/test/work/项目需求.md",
      n_chunks: 3,
    },
    {
      doc_id: "doc-upload-1",
      title: "测试数据沟通.pdf",
      kind: "pdf",
      source: "upload",
      source_path: null,
      n_chunks: 8,
    },
    {
      doc_id: "doc-meeting-1",
      title: "找人联络及测试数据沟通",
      kind: "meeting",
      source: "meeting",
      source_path: null,
      n_chunks: 12,
    },
  ];

  await page.route(/\/(api\/)?workspace\/status$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        configured_dirs: ["/Users/test/work"],
        authorized_dirs: ["/Users/test/work"],
        n_indexed: 1,
        max_file_mb: 100,
        scan_on_startup: true,
      }),
    });
  });

  await page.route(/\/(api\/)?rag\/docs(\/[^?]+)?$/, async (route) => {
    const req = route.request();
    if (req.method() === "DELETE") {
      deletedDocId = req.url().split("/").pop() ?? null;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "deleted", doc_id: deletedDocId }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total: docs.length,
        by_source: {
          workspace: [docs[0]],
          upload: [docs[1]],
          meeting: [docs[2]],
        },
        docs,
      }),
    });
  });

  await installEchoMock(page, {
    skipPaths: ["/workspace/status", "/rag/docs"],
  });
  await page.goto("/");

  await page.getByTestId("workspace-dirs-tag").click();
  await expect(page.getByText("管理知识库")).toBeVisible();
  const docList = page.getByTestId("knowledge-doc-list");
  await expect(docList).toBeVisible();
  await expect(docList.getByText("项目需求.md", { exact: true })).toBeVisible();
  await expect(docList.getByText("测试数据沟通.pdf", { exact: true })).toBeVisible();
  await expect(docList.getByText("找人联络及测试数据沟通", { exact: true })).toBeVisible();
  await expect(page.getByTestId("workspace-modal-add-dir")).toBeVisible();

  await page.getByTestId("knowledge-doc-delete-doc-ws-1").click();
  const confirm = page.locator(".ant-modal-confirm").filter({ hasText: "删除这条知识库文档？" });
  await expect(confirm).toBeVisible();
  await confirm.locator(".ant-modal-confirm-btns .ant-btn-primary").click();
  await expect.poll(() => deletedDocId).toBe("doc-ws-1");
});

test("工作区配置入口直达设置里的添加目录按钮", async ({ page }) => {
  // 故意让工作区数据晚于 Drawer 动画返回，覆盖 headed/慢机器上的真实时序。
  await page.route(/\/(api\/)?workspace\/status$/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 450));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        configured_dirs: [],
        authorized_dirs: [],
        n_indexed: 0,
        max_file_mb: 100,
        scan_on_startup: true,
      }),
    });
  });
  await installEchoMock(page, { skipPaths: ["/workspace/status"] });
  await page.goto("/");

  await page.getByTestId("workspace-config-btn").click();
  await expect(page.getByText("管理知识库")).toBeVisible();
  await page.getByTestId("workspace-modal-add-dir").click();
  await expect(page.getByTestId("workspace-settings-section")).toBeVisible();
  await expect(page.getByRole("dialog", { name: "管理知识库" })).toBeHidden();
  await expect(page.getByRole("dialog", { name: "设置" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(page.getByTestId("workspace-add-dir")).toBeVisible();
  await expect(page.getByTestId("workspace-add-dir")).toBeFocused();

  await page.locator(".echodesk-settings-drawer").evaluate(async (drawer) => {
    await Promise.all(
      drawer.getAnimations({ subtree: true }).map((animation) =>
        animation.finished.catch(() => undefined),
      ),
    );
  });
  await expect(page.getByTestId("workspace-add-dir")).toBeFocused();
});

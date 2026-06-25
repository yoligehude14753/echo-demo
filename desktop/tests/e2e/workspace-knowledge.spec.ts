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
  await expect(page.getByText("知识库 / 工作区文件")).toBeVisible();
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
  await installEchoMock(page);
  await page.goto("/");

  await page.getByTestId("workspace-config-btn").click();
  await expect(page.getByTestId("workspace-settings-section")).toBeVisible();
  await expect(page.getByTestId("workspace-add-dir")).toBeVisible();
  await expect(page.getByTestId("workspace-add-dir")).toBeFocused();
});

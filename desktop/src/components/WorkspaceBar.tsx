/**
 * WorkspaceBar：顶部状态栏，展示知识库 / 工作区配置与索引状态。
 *
 * - 配置入口：仅展示 `ECHO_WORKSPACE_DIRS` 配置的目录（后端 settings）
 * - 状态：N 目录 · M 文档 · 上次扫描结果
 * - 操作：手动触发扫描 / 清空 workspace 索引
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Button, Modal, Tag, Tooltip, message } from "antd";
import { FileText, Library, RefreshCw, Settings, Trash2 } from "lucide-react";

import {
  type RagDocSummary,
  type RagDocsResponse,
  type WorkspaceStatus,
  deleteRagDoc,
  listRagDocs,
  workspaceClear,
  workspaceScan,
  workspaceStatus,
} from "@/api";

interface Props {
  onOpenSettings?: () => void;
}

export default function WorkspaceBar({ onOpenSettings }: Props): JSX.Element {
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [docs, setDocs] = useState<RagDocsResponse | null>(null);
  const [scanning, setScanning] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [deletingDocId, setDeletingDocId] = useState<string | null>(null);
  const openSettingsAfterCloseRef = useRef(false);

  const requestSettingsFromModal = useCallback(() => {
    if (!onOpenSettings) return;
    openSettingsAfterCloseRef.current = true;
    setModalOpen(false);
  }, [onOpenSettings]);

  const handleModalOpenChange = useCallback(
    (open: boolean) => {
      if (open || !openSettingsAfterCloseRef.current) return;
      openSettingsAfterCloseRef.current = false;
      onOpenSettings?.();
    },
    [onOpenSettings],
  );

  const refresh = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([workspaceStatus(), listRagDocs()]);
      setStatus(s);
      setDocs(d);
    } catch (e) {
      // 后端不可达时静默；状态栏只是辅助信息
      void e;
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 30_000); // 30s 轮询，足够低频
    return () => clearInterval(t);
  }, [refresh]);

  const onScan = useCallback(async () => {
    setScanning(true);
    try {
      const r = await workspaceScan();
      const text = `更新完成：新增 ${r.n_added} · 更新 ${r.n_updated} · 未变更 ${r.n_skipped} · 移除 ${r.n_removed} · 失败 ${r.n_failed}`;
      if (r.n_failed > 0) {
        message.warning(text);
      } else {
        message.success(text);
      }
      await refresh();
    } catch (e) {
      console.error("[workspace] scan failed", e);
      message.error("扫描失败，请检查目录权限后重试");
    } finally {
      setScanning(false);
    }
  }, [refresh]);

  const onClear = useCallback(async () => {
    Modal.confirm({
      title: "清空工作区索引？",
      content: "仅清除来自工作区扫描的文档；上传 / 会议来源的索引保留。",
      okText: "清空",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const r = await workspaceClear();
          message.success(`已清除 ${r.n_removed} 个工作区文档`);
          await refresh();
        } catch (e) {
          console.error("[workspace] clear index failed", e);
          message.error("知识库清空失败，请重试");
        }
      },
    });
  }, [refresh]);

  const onDeleteDoc = useCallback((doc: RagDocSummary) => {
    Modal.confirm({
      title: "删除这条知识库文档？",
      content: (
        <div className="text-[12px] leading-relaxed">
          <div className="font-medium text-ink-800">{doc.title}</div>
          <div className="mt-1 text-ink-500">
            只会从本地知识库索引移除，不删除原始文件。
          </div>
        </div>
      ),
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setDeletingDocId(doc.doc_id);
        try {
          await deleteRagDoc(doc.doc_id);
          message.success("已从知识库移除");
          await refresh();
        } catch (e) {
          console.error("[workspace] delete document failed", e);
          message.error("文档移除失败，请重试");
          throw e;
        } finally {
          setDeletingDocId(null);
        }
      },
    });
  }, [refresh]);

  const openKnowledgeModalFromKeyboard = useCallback(
    (e: KeyboardEvent<HTMLSpanElement>) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      setModalOpen(true);
    },
    [],
  );

  const nDirs = status?.authorized_dirs.length ?? 0;
  const nIndexed = status?.n_indexed ?? 0;
  const nDocsTotal = docs?.total ?? 0;
  const nUploadDocs = docs?.by_source?.upload?.length ?? 0;
  const nMeetingDocs = docs?.by_source?.meeting?.length ?? 0;
  const configuredNotAuthorized =
    (status?.configured_dirs.length ?? 0) - nDirs;

  return (
    <>
      <div
        className="flex items-center gap-2 px-4 h-8 text-[11px] text-ink-600 border-b border-paper-300 bg-paper-100"
        data-testid="workspace-bar"
      >
        <Library className="workspace-icon w-3 h-3" />
        <span className="workspace-title font-medium">知识库</span>

        <Tooltip
          title={
            status?.authorized_dirs.length
              ? status.authorized_dirs.join("\n")
              : "还没有知识库目录；添加后 EchoDesk 会自动收录其中的文件"
          }
        >
          <Tag
            color={nDirs > 0 ? "blue" : "default"}
            className="!m-0 cursor-pointer"
            onClick={() => setModalOpen(true)}
            onKeyDown={openKnowledgeModalFromKeyboard}
            role="button"
            tabIndex={0}
            data-testid="workspace-dirs-tag"
            aria-label="打开知识库和工作区文件"
          >
            {nDirs} 目录
          </Tag>
        </Tooltip>

        {configuredNotAuthorized > 0 && (
          <Tooltip title="部分配置目录不存在或不可读">
            <Tag color="warning" className="!m-0">
              {configuredNotAuthorized} 失效
            </Tag>
          </Tooltip>
        )}

        <Tooltip title="已建立索引的工作区文档数">
          <Tag color={nIndexed > 0 ? "geekblue" : "default"} className="!m-0">
            {nIndexed} 文档
          </Tag>
        </Tooltip>

        <span className="sr-only" data-testid="workspace-upload-count">
          上传 {nUploadDocs} · 会议 {nMeetingDocs} · 总计 {nDocsTotal}
        </span>

        <div className="workspace-actions ml-auto flex items-center gap-1">
          <Button
            size="small"
            type="default"
            icon={<Library className="w-3 h-3" />}
            onClick={() => setModalOpen(true)}
            data-testid="workspace-config-btn"
            aria-label="管理知识库"
          >
            <span>管理</span>
          </Button>
        </div>
      </div>

      <Modal
        title="管理知识库"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        afterOpenChange={handleModalOpenChange}
        footer={null}
        width={760}
        destroyOnHidden
      >
        <div className="text-[13px] space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Tag color={nDirs > 0 ? "blue" : "default"} className="!m-0">
              {nDirs} 目录
            </Tag>
            <Tag color={nIndexed > 0 ? "geekblue" : "default"} className="!m-0">
              {nIndexed} 工作区文档
            </Tag>
            <Tag color={nDocsTotal > 0 ? "purple" : "default"} className="!m-0">
              总计 {nDocsTotal}
            </Tag>
            <Button
              size="small"
              type="text"
              icon={<RefreshCw className="w-3 h-3" />}
              onClick={() => void refresh()}
              data-testid="workspace-modal-refresh"
            >
              刷新
            </Button>
            <Button
              size="small"
              type="text"
              icon={
                <RefreshCw
                  className={`w-3 h-3 ${scanning ? "animate-spin" : ""}`}
                />
              }
              onClick={() => void onScan()}
              disabled={scanning || nDirs === 0}
              data-testid="workspace-scan-btn"
            >
              {scanning ? "更新中" : "更新索引"}
            </Button>
            {onOpenSettings && (
              <Button
                size="small"
                type="text"
                icon={<Settings className="w-3 h-3" />}
                onClick={requestSettingsFromModal}
                data-testid="workspace-open-settings"
              >
                配置目录
              </Button>
            )}
            <Button
              size="small"
              type="text"
              danger
              icon={<Trash2 className="w-3 h-3" />}
              onClick={() => void onClear()}
              disabled={nIndexed === 0}
              data-testid="workspace-clear-btn"
            >
              清除工作区索引
            </Button>
          </div>

          <div>
            <div className="font-medium mb-1">目录配置</div>
            <div className="rounded-md border border-paper-300 bg-paper-100 p-3 text-[12px] text-ink-600 leading-relaxed">
              <div>
                推荐直接在设置里添加目录，支持系统目录选择器，添加后可立即重扫。
              </div>
              {onOpenSettings && (
                <Button
                  className="!mt-2"
                  size="small"
                  type="primary"
                  icon={<Settings className="w-3 h-3" />}
                  onClick={requestSettingsFromModal}
                  data-testid="workspace-modal-add-dir"
                >
                  去添加工作区目录
                </Button>
              )}
            </div>
          </div>

          <div>
            <div className="font-medium mb-1">已授权目录（{nDirs}）</div>
            {status?.authorized_dirs.length ? (
              <ul className="list-disc pl-5 text-[12px] space-y-1">
                {status.authorized_dirs.map((d) => (
                  <li key={d} className="font-mono break-all">
                    {d}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-[12px] text-ink-500">
                当前未配置或配置的目录均不存在。
              </div>
            )}
          </div>

          {status?.configured_dirs.length !== status?.authorized_dirs.length && (
            <div>
              <div className="font-medium mb-1 text-warn-700">
                配置但不可用的目录
              </div>
              <ul className="list-disc pl-5 text-[12px] space-y-1">
                {(status?.configured_dirs ?? [])
                  .filter((d) => !status?.authorized_dirs.includes(d))
                  .map((d) => (
                    <li
                      key={d}
                      className="font-mono break-all text-ink-500 line-through"
                    >
                      {d}
                    </li>
                  ))}
              </ul>
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-1">
              <div className="font-medium">已收录文档（{nDocsTotal}）</div>
              <div className="text-[11px] text-ink-500">
                上传 {nUploadDocs} · 会议 {nMeetingDocs} · 工作区 {nIndexed}
              </div>
            </div>
            <KnowledgeDocList
              docs={docs?.docs ?? []}
              deletingDocId={deletingDocId}
              onDelete={onDeleteDoc}
            />
          </div>

          <div>
            <div className="font-medium mb-1">支持的文件类型</div>
            <div className="text-[12px] text-ink-700 leading-relaxed">
              二进制：PDF · docx · pptx · xlsx · html · csv · epub · msg · eml
              <br />
              文本类：md · txt · json · yaml · xml · log · py · ts · sql 等
            </div>
          </div>
        </div>
      </Modal>
    </>
  );
}

function KnowledgeDocList({
  docs,
  deletingDocId,
  onDelete,
}: {
  docs: RagDocSummary[];
  deletingDocId: string | null;
  onDelete: (doc: RagDocSummary) => void;
}): JSX.Element {
  if (docs.length === 0) {
    return (
      <div className="border border-dashed border-paper-300 rounded-md p-4 text-center text-[12px] text-ink-500">
        还没有收录文档。可将文件拖到输入框，或添加一个文件夹。
      </div>
    );
  }

  return (
    <div
      className="border border-paper-300 rounded-md divide-y divide-paper-200 max-h-[260px] overflow-y-auto"
      data-testid="knowledge-doc-list"
    >
      {docs.map((doc) => (
        <div
          key={doc.doc_id}
          className="flex items-start gap-2 px-2.5 py-2 bg-white hover:bg-paper-100 transition"
          data-testid="knowledge-doc-row"
        >
          <FileText className="w-3.5 h-3.5 text-ink-400 mt-0.5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="text-[12px] font-medium text-ink-800 truncate">
                {doc.title?.trim() || "未命名文档"}
              </span>
              <Tag color={sourceColor(doc.source)} className="!m-0 !text-[10px]">
                {sourceLabel(doc.source)}
              </Tag>
              <Tag className="!m-0 !text-[10px]">{documentKindLabel(doc.kind)}</Tag>
            </div>
            <div
              className="mt-0.5 text-[11px] text-ink-500 truncate"
              title={doc.source_path ?? sourceLabel(doc.source)}
            >
              {doc.source_path ?? sourceLabel(doc.source)}
            </div>
            <div className="mt-0.5 text-[10px] text-ink-400">
              已提取 {doc.n_chunks} 个内容片段
            </div>
          </div>
          <Button
            size="small"
            type="text"
            danger
            icon={<Trash2 className="w-3 h-3" />}
            loading={deletingDocId === doc.doc_id}
            onClick={() => onDelete(doc)}
            aria-label={`删除知识库文档 ${doc.title?.trim() || "未命名文档"}`}
            data-testid={`knowledge-doc-delete-${doc.doc_id}`}
          />
        </div>
      ))}
    </div>
  );
}

function sourceLabel(source: string): string {
  switch (source) {
    case "workspace":
      return "工作区";
    case "upload":
      return "上传";
    case "meeting":
      return "会议";
    case "ambient":
      return "环境记忆";
    default:
      return "其他";
  }
}

function documentKindLabel(kind: string): string {
  const normalized = kind.toLocaleLowerCase();
  const labels: Record<string, string> = {
    meeting: "会议记录",
    markdown: "Markdown",
    md: "Markdown",
    txt: "文本",
    text: "文本",
    doc: "Word",
    docx: "Word",
    xls: "Excel",
    xlsx: "Excel",
    ppt: "PPT",
    pptx: "PPT",
    pdf: "PDF",
    html: "网页",
    csv: "CSV",
  };
  return labels[normalized] ?? "文档";
}

function sourceColor(source: string): string {
  switch (source) {
    case "workspace":
      return "geekblue";
    case "upload":
      return "purple";
    case "meeting":
      return "cyan";
    case "ambient":
      return "green";
    default:
      return "default";
  }
}

/**
 * WorkspaceBar：顶部状态栏，展示知识库 / 工作区配置与索引状态。
 *
 * - 配置入口：仅展示 `ECHO_WORKSPACE_DIRS` 配置的目录（后端 settings）
 * - 状态：N 目录 · M 文档 · 上次扫描结果
 * - 操作：手动触发扫描 / 清空 workspace 索引
 */

import { useCallback, useEffect, useState } from "react";
import type { KeyboardEvent } from "react";
import { Button, Modal, Tag, Tooltip, message } from "antd";
import { FileText, FolderOpen, RefreshCw, Settings, Trash2 } from "lucide-react";

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
      message.success(
        `扫描完成：新增 ${r.n_added} · 更新 ${r.n_updated} · 跳过 ${r.n_skipped} · 删除 ${r.n_removed} · 耗时 ${r.duration_s}s`,
      );
      await refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`扫描失败：${msg}`);
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
          const msg = e instanceof Error ? e.message : String(e);
          message.error(`清空失败：${msg}`);
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
          const msg = e instanceof Error ? e.message : String(e);
          message.error(`删除失败：${msg}`);
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
        <FolderOpen className="workspace-icon w-3 h-3" />
        <span className="workspace-title font-medium">工作区</span>

        <Tooltip
          title={
            status?.authorized_dirs.length
              ? status.authorized_dirs.join("\n")
              : "未配置；在 backend .env 设置 WORKSPACE_DIRS=路径1,路径2 后重启 backend"
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

        <Tooltip title="工作区扫描入库的文档数">
          <Tag color={nIndexed > 0 ? "geekblue" : "default"} className="!m-0">
            {nIndexed} 工作区文档
          </Tag>
        </Tooltip>

        <span className="text-ink-400">·</span>

        <Tooltip title="用户上传 / 会议生成的文档（聊天框拖入即可）">
          <span data-testid="workspace-upload-count">
            上传 {nUploadDocs} · 会议 {nMeetingDocs} · 总计 {nDocsTotal}
          </span>
        </Tooltip>

        <div className="workspace-actions ml-auto flex items-center gap-1">
          <Button
            size="small"
            type="default"
            icon={<Settings className="w-3 h-3" />}
            onClick={() => {
              if (onOpenSettings) {
                onOpenSettings();
              } else {
                setModalOpen(true);
              }
            }}
            data-testid="workspace-config-btn"
          >
            配置工作区
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
            {scanning ? "扫描中" : "扫描"}
          </Button>
          <Button
            size="small"
            type="text"
            danger
            icon={<Trash2 className="w-3 h-3" />}
            onClick={() => void onClear()}
            disabled={nIndexed === 0}
            data-testid="workspace-clear-btn"
          >
            清空
          </Button>
        </div>
      </div>

      <Modal
        title="知识库 / 工作区文件"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        footer={null}
        width={760}
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
            {onOpenSettings && (
              <Button
                size="small"
                type="text"
                icon={<Settings className="w-3 h-3" />}
                onClick={() => {
                  setModalOpen(false);
                  onOpenSettings();
                }}
                data-testid="workspace-open-settings"
              >
                配置目录
              </Button>
            )}
          </div>

          <div>
            <div className="font-medium mb-1">目录配置</div>
            <pre className="bg-paper-100 rounded p-2 text-[11px] leading-snug">
              {`# 在 .env 中设置（多目录用逗号分隔）
WORKSPACE_DIRS=~/Documents/work,~/Notes
WORKSPACE_MAX_FILE_MB=100
WORKSPACE_SCAN_ON_STARTUP=true`}
            </pre>
            <div className="text-[11px] text-ink-500">
              也可以点右上角齿轮进入设置，用系统目录选择器添加目录并立即扫描。
            </div>
          </div>

          <div>
            <div className="font-medium mb-1">可扫描目录（{nDirs}）</div>
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
              <div className="font-medium">已入库文档（{nDocsTotal}）</div>
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
        暂无已入库文档。拖文件到输入框，或在设置里添加工作区目录后扫描。
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
                {doc.title || doc.doc_id}
              </span>
              <Tag color={sourceColor(doc.source)} className="!m-0 !text-[10px]">
                {sourceLabel(doc.source)}
              </Tag>
              <Tag className="!m-0 !text-[10px]">{doc.kind}</Tag>
            </div>
            <div className="mt-0.5 text-[11px] text-ink-500 truncate" title={doc.source_path ?? doc.doc_id}>
              {doc.source_path ?? doc.doc_id}
            </div>
            <div className="mt-0.5 text-[10px] text-ink-400">
              {doc.n_chunks} chunks · {doc.doc_id}
            </div>
          </div>
          <Button
            size="small"
            type="text"
            danger
            icon={<Trash2 className="w-3 h-3" />}
            loading={deletingDocId === doc.doc_id}
            onClick={() => onDelete(doc)}
            aria-label={`删除知识库文档 ${doc.title || doc.doc_id}`}
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
      return source || "未知";
  }
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

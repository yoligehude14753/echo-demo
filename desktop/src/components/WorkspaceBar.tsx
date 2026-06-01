/**
 * WorkspaceBar：顶部状态栏，展示授权工作区配置与索引状态。
 *
 * - 配置入口：展示已授权目录；添加/移除目录在设置面板完成
 * - 状态：N 目录 · M 文档 · 上次扫描结果
 * - 操作：手动触发扫描 / 清空 workspace 索引
 */

import { useCallback, useEffect, useState } from "react";
import { Button, Modal, Tag, Tooltip, message } from "antd";
import { FolderOpen, RefreshCw, Trash2 } from "lucide-react";

import {
  type RagDocsResponse,
  type WorkspaceStatus,
  listRagDocs,
  workspaceClear,
  workspaceScan,
  workspaceStatus,
} from "@/api";

export default function WorkspaceBar(): JSX.Element {
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [docs, setDocs] = useState<RagDocsResponse | null>(null);
  const [scanning, setScanning] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);

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
      content: "仅清除来自授权工作区扫描的文档；上传 / 会议来源的索引保留。",
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
        <FolderOpen className="w-3 h-3" />
        <span className="font-medium">工作区</span>

        <Tooltip
          title={
            status?.authorized_dirs.length
              ? status.authorized_dirs.join("\n")
              : "未添加工作区目录；打开设置 → 工作区目录可添加"
          }
        >
          <Tag
            color={nDirs > 0 ? "blue" : "default"}
            className="!m-0 cursor-pointer"
            onClick={() => setModalOpen(true)}
            data-testid="workspace-dirs-tag"
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

        <div className="ml-auto flex items-center gap-1">
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
        title="授权工作区配置"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        footer={null}
        width={580}
      >
        <div className="text-[13px] space-y-3">
          <div>
            <div className="font-medium mb-1">配置方式</div>
            <div className="bg-paper-100 rounded p-2 text-[12px] leading-relaxed text-ink-600">
              打开右上角「设置」→「工作区目录」，选择需要纳入知识库的文件夹。
              添加后 EchoDesk 会在后台扫描，聊天时可引用这些文档回答。
            </div>
            <div className="text-[11px] text-ink-500">
              这里展示当前索引状态，也可以手动重新扫描或清空工作区索引。
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

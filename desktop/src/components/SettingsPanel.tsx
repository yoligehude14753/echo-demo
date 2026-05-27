/**
 * SettingsPanel · Phase 2 P2.5 + P2.6 frontend
 *
 * 抽屉式设置面板，三个能力：
 *  - 显示 ~/.echodesk/ 数据目录大小 + 子目录 breakdown
 *  - 导出诊断包 zip（一键下载）
 *  - 重置 speaker registry（保留 transcript）
 *
 * 由 App.tsx header 齿轮按钮触发；用 antd Drawer 从右边滑出，不阻断主工作流。
 *
 * 数据接口（依赖 phase2-admin-diagnostics backend PR）：
 *  GET  /admin/data-dir
 *  GET  /admin/diagnostics/export
 *  POST /admin/speakers/reset
 */

import { Drawer, Button, Modal, message, Spin, Tooltip } from "antd";
import {
  Database,
  Download,
  FolderOpen,
  RefreshCw,
  Users,
  AlertTriangle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { apiUrl } from "@/runtime";

interface DataDirBreakdown {
  db: number;
  storage: number;
  rag_index: number;
  logs: number;
  skill_build: number;
}

interface DataDirResponse {
  path: string;
  exists: boolean;
  size_bytes: number;
  breakdown: DataDirBreakdown;
}

interface SpeakerResetResponse {
  speakers_deleted: number;
  segments_cleared: number;
  diarizer_reset: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** P3.1：让用户在设置里"重新看一次引导"。可选，缺省时不显示按钮。 */
  onReplayOnboarding?: () => void;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

const BREAKDOWN_LABELS: Array<{
  key: keyof DataDirBreakdown;
  label: string;
  hint: string;
}> = [
  { key: "db", label: "数据库", hint: "echodesk.db (会议/段/说话人)" },
  { key: "storage", label: "音频/产物", hint: "录音 wav + @生成 的 PPT/Word/Excel/HTML" },
  { key: "rag_index", label: "RAG 索引", hint: "BM25 倒排索引" },
  { key: "logs", label: "日志", hint: "backend.log 按天 rotate，保留 14 天" },
  { key: "skill_build", label: "Skill 工作目录", hint: "@生成 临时构建目录" },
];

export default function SettingsPanel({
  open,
  onClose,
  onReplayOnboarding,
}: Props): JSX.Element {
  const [dataDir, setDataDir] = useState<DataDirResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [diagBusy, setDiagBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);

  const refreshDataDir = useCallback(async () => {
    setLoading(true);
    try {
      const url = await apiUrl("/admin/data-dir");
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as DataDirResponse;
      setDataDir(json);
    } catch (e) {
      message.error(`读取数据目录失败：${(e as Error).message}`);
      setDataDir(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void refreshDataDir();
  }, [open, refreshDataDir]);

  const onOpenDataDir = async () => {
    if (!dataDir?.path) return;
    try {
      await navigator.clipboard.writeText(dataDir.path);
      message.success(`已复制路径到剪贴板：${dataDir.path}`);
    } catch {
      message.info(`数据目录：${dataDir.path}`);
    }
  };

  const onDownloadDiagnostics = async () => {
    setDiagBusy(true);
    try {
      const url = await apiUrl("/admin/diagnostics/export");
      const a = document.createElement("a");
      a.href = url;
      a.download = `echodesk-diag-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      message.success("诊断包下载中…");
    } catch (e) {
      message.error(`导出失败：${(e as Error).message}`);
    } finally {
      setDiagBusy(false);
    }
  };

  const onResetSpeakers = () => {
    Modal.confirm({
      title: "重置说话人？",
      icon: <AlertTriangle className="w-4 h-4 text-amber-500" />,
      content: (
        <div className="text-[12px] leading-relaxed">
          将清空 <b>所有说话人识别数据</b>（speakers / centroid），但
          <b>保留所有转写文字</b>。下次录音时 diarizer 将从头学习。
          <br />
          <br />
          适用场景：speaker registry 被噪音污染（"说话人 86" 实际只 3 人）。
        </div>
      ),
      okText: "确认重置",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setResetBusy(true);
        try {
          const url = await apiUrl("/admin/speakers/reset");
          const res = await fetch(url, { method: "POST" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = (await res.json()) as SpeakerResetResponse;
          message.success(
            `已重置：清空 ${json.speakers_deleted} 个说话人 · 清理 ${json.segments_cleared} 段引用 · diarizer ${json.diarizer_reset ? "已重置" : "未重置"}`,
          );
          void refreshDataDir();
        } catch (e) {
          message.error(`重置失败：${(e as Error).message}`);
        } finally {
          setResetBusy(false);
        }
      },
    });
  };

  return (
    <Drawer
      title={<span className="text-[14px] font-semibold">设置</span>}
      placement="right"
      width={420}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      <div className="space-y-5 text-[13px]">
        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Database className="w-4 h-4" />
            <span>数据</span>
            <Tooltip title="刷新">
              <button
                type="button"
                onClick={() => void refreshDataDir()}
                className="ml-auto text-ink-400 hover:text-ink-700"
                aria-label="刷新数据目录"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
              </button>
            </Tooltip>
          </div>
          <div className="bg-paper-150 rounded-md p-3 space-y-2">
            {loading && !dataDir ? (
              <Spin size="small" />
            ) : !dataDir ? (
              <div className="text-ink-400 text-[12px]">读取失败</div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] text-ink-600 truncate">
                    {dataDir.path}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    icon={<FolderOpen className="w-3.5 h-3.5" />}
                    onClick={() => void onOpenDataDir()}
                    data-testid="open-data-dir"
                  >
                    复制路径
                  </Button>
                </div>
                <div className="flex items-center justify-between text-[12px] py-1 border-b border-paper-300">
                  <span className="font-medium">总占用</span>
                  <span className="font-mono text-accent">
                    {fmtBytes(dataDir.size_bytes)}
                  </span>
                </div>
                {BREAKDOWN_LABELS.map(({ key, label, hint }) => (
                  <div
                    key={key}
                    className="flex items-center justify-between text-[11px] text-ink-600"
                    title={hint}
                  >
                    <span>{label}</span>
                    <span className="font-mono">
                      {fmtBytes(dataDir.breakdown[key] ?? 0)}
                    </span>
                  </div>
                ))}
              </>
            )}
          </div>
        </section>

        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Download className="w-4 h-4" />
            <span>诊断</span>
          </div>
          <Button
            block
            icon={<Download className="w-3.5 h-3.5" />}
            loading={diagBusy}
            onClick={() => void onDownloadDiagnostics()}
            data-testid="download-diagnostics"
          >
            导出诊断包 (.zip)
          </Button>
          <div className="text-[11px] text-ink-500 mt-1.5 leading-relaxed">
            包含：最近 7 天 backend log（≤5MB/文件）· 配置（API key
            已脱敏）· DB schema · 远程探针历史。报 bug 时把这个 zip 发给我们。
          </div>
        </section>

        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Users className="w-4 h-4" />
            <span>说话人管理</span>
          </div>
          <Button
            block
            danger
            icon={<AlertTriangle className="w-3.5 h-3.5" />}
            loading={resetBusy}
            onClick={onResetSpeakers}
            data-testid="reset-speakers"
          >
            重置说话人（保留转写）
          </Button>
          <div className="text-[11px] text-ink-500 mt-1.5 leading-relaxed">
            清空 speakers 表和 diarizer 内存，<b>转写文字保留</b>。下次录音
            重新学习。常用于 speaker 数量被噪音污染时。
          </div>
        </section>

        {onReplayOnboarding && (
          <section>
            <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
              <RefreshCw className="w-4 h-4" />
              <span>引导</span>
            </div>
            <Button
              block
              onClick={() => {
                onReplayOnboarding();
                onClose();
              }}
              data-testid="replay-onboarding"
            >
              重新看一次引导
            </Button>
            <div className="text-[11px] text-ink-500 mt-1.5">
              重新打开欢迎引导（数据目录、麦克风权限、@命令使用提示）。
            </div>
          </section>
        )}

        <section className="pt-3 border-t border-paper-300">
          <div className="text-[11px] text-ink-500 leading-relaxed">
            <div>EchoDesk · 独立桌面 AI 会议助手</div>
            <div className="mt-1">
              配置文件：
              <span className="font-mono text-ink-600">~/.echodesk/config.json</span>
            </div>
            <div className="mt-1">
              卸载：终端运行
              <span className="font-mono text-ink-600 ml-1">
                bash scripts/install-backend.sh --uninstall
              </span>
            </div>
          </div>
        </section>
      </div>
    </Drawer>
  );
}

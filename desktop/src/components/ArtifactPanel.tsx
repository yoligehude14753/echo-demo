import { type MouseEvent, useEffect, useState } from "react";
import { message, Modal } from "antd";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Download,
  FileCode,
  FileText,
  FileType2,
  FileSpreadsheet,
  Globe,
  Presentation,
  RotateCcw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import {
  artifactDownloadUrl,
  cancelAgentTask,
  generateArtifact,
  grantAgentRunnerAndResume,
  listAgentTasks,
  listArtifacts,
  type ArtifactKind,
} from "@/api";
import { apiUrl } from "@/runtime";
import { useStore } from "@/store";
import type { AgentTaskCard, GeneratedArtifact } from "@/types";
import { formatRelativeTime, type FailedArtifact } from "@/lib/failedArtifact";
import ArtifactPreviewModal from "@/components/ArtifactPreviewModal";

/**
 * outputs 面板：展示历史产物列表（只读）+ 7 类 in-app 预览。
 *
 * 2026-05 修订（P4.1 M4）：
 * - 全部 7 类（html / pptx / xlsx / word / markdown / pdf / txt）整条都可点击预览
 *   - pptx 走 Electron shell.openPath → Keynote；其他类型在 Modal 内渲染
 * - 顶栏新增「清空 outputs」按钮（confirm 后清空 store.artifacts；不动失败卡片）
 * - 单条 hover 显示「×」删除按钮（不二次确认，单条删错代价低）
 * - 标题主、artifact_id 副（title 缺失时退化为 artifact_id）
 */
const typeIcon: Record<string, JSX.Element> = {
  word: <FileText className="w-3.5 h-3.5" />,
  docx: <FileText className="w-3.5 h-3.5" />,
  xlsx: <FileSpreadsheet className="w-3.5 h-3.5" />,
  excel: <FileSpreadsheet className="w-3.5 h-3.5" />,
  pptx: <Presentation className="w-3.5 h-3.5" />,
  ppt: <Presentation className="w-3.5 h-3.5" />,
  html: <Globe className="w-3.5 h-3.5" />,
  markdown: <FileCode className="w-3.5 h-3.5" />,
  md: <FileCode className="w-3.5 h-3.5" />,
  pdf: <FileType2 className="w-3.5 h-3.5" />,
  txt: <FileText className="w-3.5 h-3.5" />,
  text: <FileText className="w-3.5 h-3.5" />,
};

const typeBadge: Record<string, string> = {
  word: "bg-blue-50 text-blue-700",
  docx: "bg-blue-50 text-blue-700",
  xlsx: "bg-emerald-50 text-emerald-700",
  excel: "bg-emerald-50 text-emerald-700",
  pptx: "bg-amber-50 text-amber-700",
  ppt: "bg-amber-50 text-amber-700",
  html: "bg-violet-50 text-violet-700",
  markdown: "bg-sky-50 text-sky-700",
  md: "bg-sky-50 text-sky-700",
  pdf: "bg-rose-50 text-rose-700",
  txt: "bg-paper-200 text-ink-700",
  text: "bg-paper-200 text-ink-700",
};

export default function ArtifactPanel(): JSX.Element {
  const globalArtifacts = useStore((s) => s.artifacts);
  const agentTasks = useStore((s) => s.agentTasks);
  const upsertAgentTask = useStore((s) => s.upsertAgentTask);
  const failedArtifacts = useStore((s) => s.failedArtifacts);
  const dismissFailedArtifact = useStore((s) => s.dismissFailedArtifact);
  const clearArtifacts = useStore((s) => s.clearArtifacts);
  const removeArtifact = useStore((s) => s.removeArtifact);
  const addArtifact = useStore((s) => s.addArtifact);
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const [previewArtifact, setPreviewArtifact] =
    useState<GeneratedArtifact | null>(null);

  useEffect(() => {
    if (!connected) return;
    let alive = true;
    void (async (): Promise<void> => {
      const [restoredResult, tasksResult] = await Promise.allSettled([
        listArtifacts(500),
        listAgentTasks(50),
      ]);
      if (!alive) return;
      if (restoredResult.status === "fulfilled") {
        const restored = restoredResult.value;
        restored
          .slice()
          .reverse()
          .forEach((artifact) => addArtifact(artifact));
      }
      if (tasksResult.status === "fulfilled") {
        tasksResult.value.forEach((task) => upsertAgentTask(task));
      }
      if (restoredResult.status === "rejected" || tasksResult.status === "rejected") {
        console.warn("[artifact-panel] restore outputs partially failed:", {
          artifacts: restoredResult.status === "rejected" ? restoredResult.reason : null,
          tasks: tasksResult.status === "rejected" ? tasksResult.reason : null,
        });
      }
    })();
    return () => {
      alive = false;
    };
  }, [addArtifact, connected, upsertAgentTask]);

  // outputs 是全局工作产物，不应因为用户正在查看某个历史会议而消失。
  // 会议关联产物只作为补充来源合并进列表；全局新生成产物始终可见。
  const meetingArtifacts = meeting?.artifacts ?? [];
  const globalIds = new Set(globalArtifacts.map((a) => a.artifact_id));
  const artifacts = [
    ...globalArtifacts,
    ...meetingArtifacts.filter((a) => !globalIds.has(a.artifact_id)),
  ];
  const visibleFailed = failedArtifacts;
  const visibleAgentTasks = Object.values(agentTasks)
    .sort((a, b) => (b.submitted_at ?? "").localeCompare(a.submitted_at ?? ""))
    .slice(0, 20);

  function onClearAll(): void {
    Modal.confirm({
      title: "清空 outputs",
      content: `确定清空 ${globalArtifacts.length} 条历史产物？该操作不可撤回（文件本身仍保留在磁盘）。`,
      okText: "清空",
      okType: "danger",
      cancelText: "取消",
      onOk: () => clearArtifacts(),
    });
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-paper-50">
      <div className="flex items-center justify-between px-6 h-11 border-b border-paper-300 shrink-0">
        <span className="text-[13px] text-ink-700 font-medium lowercase tracking-wider">
          outputs
        </span>
        <span className="flex items-center gap-2">
          <span className="text-[11px] text-ink-400">
            {visibleAgentTasks.length + artifacts.length}
            {visibleFailed.length > 0 ? ` · ${visibleFailed.length} 失败` : ""}
          </span>
          {globalArtifacts.length > 0 && (
            <button
              type="button"
              data-testid="clear-artifacts-btn"
              aria-label="清空 outputs"
              onClick={onClearAll}
              className="p-1 rounded text-ink-400 hover:text-err hover:bg-paper-150 transition-colors"
              title="清空 outputs"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </span>
      </div>

      <div
        className="flex-1 overflow-y-auto px-3 py-2 space-y-1"
        data-testid="artifact-list"
        data-scope="global"
      >
        {visibleFailed.map((f) => (
          <FailedArtifactCard
            key={f.id}
            failed={f}
            onDismiss={() => dismissFailedArtifact(f.id)}
          />
        ))}
        {visibleAgentTasks.map((task) => (
          <AgentTaskCardView
            key={task.task_id}
            task={task}
            onUpdate={upsertAgentTask}
          />
        ))}
        {artifacts.length === 0 && visibleFailed.length === 0 && visibleAgentTasks.length === 0 && (
          <div className="px-3 py-8 text-center text-ink-400 text-[11px] space-y-1">
            <div>暂无产物</div>
            <div className="text-ink-300">
              在输入框输入 @生成 PPT / @报告 / @Excel … 触发
            </div>
          </div>
        )}
        {artifacts.map((a) => {
          const displayName = a.title || a.artifact_id;
          const shortId = a.artifact_id.slice(0, 14);
          return (
            <div
              key={a.artifact_id}
              data-testid="artifact-card"
              data-artifact-id={a.artifact_id}
              className="group px-3 py-2.5 rounded-lg hover:bg-paper-150 cursor-pointer transition-colors"
              onClick={() => setPreviewArtifact(a)}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2 min-w-0 flex-1">
                  <span
                    className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0 ${
                      typeBadge[a.artifact_type] ?? "bg-paper-200 text-ink-700"
                    }`}
                  >
                    {typeIcon[a.artifact_type] ?? null}
                    <span className="uppercase">{a.artifact_type}</span>
                  </span>
                  <span className="flex flex-col min-w-0">
                    <span
                      className="text-[13px] text-ink-800 font-medium truncate"
                      title={a.artifact_id}
                      data-testid="artifact-title"
                    >
                      {displayName}
                    </span>
                    {a.title && (
                      <span
                        className="font-mono text-[10px] text-ink-400 truncate"
                        title={a.artifact_id}
                      >
                        {shortId}
                      </span>
                    )}
                  </span>
                </span>
                <span className="flex items-center gap-1 shrink-0">
                  <a
                    href={artifactDownloadUrl(a.artifact_id)}
                    target="_blank"
                    rel="noreferrer"
                    aria-label="下载产物"
                    onClick={(e) => e.stopPropagation()}
                    className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded hover:bg-paper-200"
                  >
                    <Download className="w-3.5 h-3.5 text-ink-500 hover:text-accent" />
                  </a>
                  <button
                    type="button"
                    data-testid="remove-artifact-btn"
                    aria-label="删除该产物"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeArtifact(a.artifact_id);
                    }}
                    className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded text-ink-400 hover:text-err hover:bg-paper-200"
                    title="从列表移除（不删磁盘文件）"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </span>
              </div>
              <div className="text-[11px] text-ink-400 mt-1 flex items-center gap-2 pl-1">
                <span>{(a.size_bytes / 1024).toFixed(1)} KB</span>
                <span>·</span>
                <span>{(a.generation_latency_ms / 1000).toFixed(1)}s</span>
                <span>·</span>
                <span className="font-mono text-[10px]">{a.model}</span>
              </div>
            </div>
          );
        })}
      </div>

      <ArtifactPreviewModal
        artifact={previewArtifact}
        onClose={() => setPreviewArtifact(null)}
      />
    </div>
  );
}

function statusLabel(state: string): string {
  switch (state) {
    case "waiting_permission":
      return "等待授权";
    case "pending":
      return "排队中";
    case "running":
      return "执行中";
    case "succeeded":
      return "已完成";
    case "failed":
      return "失败";
    case "cancelled":
      return "已取消";
    case "cancel_failed":
      return "取消失败";
    case "timeout":
      return "已超时";
    default:
      return "进行中";
  }
}

function AgentTaskCardView({
  task,
  onUpdate,
}: {
  task: AgentTaskCard;
  onUpdate: (task: AgentTaskCard) => void;
}): JSX.Element {
  const [busy, setBusy] = useState(false);
  const snapshot = task.snapshot ?? {};
  const artifacts = Array.isArray(snapshot.artifacts) ? snapshot.artifacts : task.artifacts;
  const terminal = ["succeeded", "failed", "cancelled", "cancel_failed", "timeout"].includes(task.state);
  const needsGrant = task.state === "waiting_permission";
  const failed = task.state === "failed" || task.state === "timeout" || task.state === "cancel_failed";
  const Icon = task.state === "succeeded" ? CheckCircle2 : failed ? AlertCircle : Clock3;

  async function grantAndStart(e: MouseEvent): Promise<void> {
    e.stopPropagation();
    setBusy(true);
    try {
      const res = await grantAgentRunnerAndResume(task.task_id);
      if (res.resumed_task) onUpdate(res.resumed_task);
    } finally {
      setBusy(false);
    }
  }

  async function cancel(e: MouseEvent): Promise<void> {
    e.stopPropagation();
    setBusy(true);
    try {
      onUpdate(await cancelAgentTask(task.task_id));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-testid="agent-task-card"
      data-task-id={task.task_id}
      className={`rounded-md border px-3 py-2.5 space-y-2 ${
        failed ? "border-err/30 bg-err/5" : "border-paper-300 bg-white"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex items-center gap-1.5 text-[12px] font-medium text-ink-800">
            <Icon className={`w-3.5 h-3.5 ${failed ? "text-err" : "text-accent"}`} />
            <span className="truncate">{task.title || "EchoDesk 正在执行"}</span>
          </div>
          <div className="text-[11px] text-ink-500 leading-relaxed">
            {String(snapshot.progress_text ?? task.progress_text ?? statusLabel(task.state))}
          </div>
        </div>
        <span className="shrink-0 rounded bg-paper-150 px-1.5 py-0.5 text-[10px] text-ink-500">
          {statusLabel(task.state)}
        </span>
      </div>

      {typeof snapshot.final_text === "string" && snapshot.final_text.length > 0 && (
        <div className="rounded bg-paper-100 px-2 py-1.5 text-[11px] text-ink-700 leading-relaxed">
          {snapshot.final_text}
        </div>
      )}

      {typeof snapshot.error === "string" && snapshot.error.length > 0 && (
        <div className="text-[11px] text-err leading-relaxed">{snapshot.error}</div>
      )}

      {artifacts.length > 0 && (
        <div className="space-y-1">
          {artifacts.map((item, idx) => {
            const art = item as { name?: string; url?: string; kind?: string };
            return (
              <AgentArtifactLink
                key={`${art.name ?? "artifact"}-${idx}`}
                item={art}
              />
            );
          })}
        </div>
      )}

      {(needsGrant || (!terminal && task.state !== "pending")) && (
        <div className="flex justify-end gap-2 pt-0.5">
          {needsGrant && (
            <button
              type="button"
              disabled={busy}
              onClick={grantAndStart}
              className="inline-flex items-center gap-1 rounded border border-accent/30 px-2 py-1 text-[11px] text-accent hover:bg-accent/10 disabled:opacity-50"
            >
              <ShieldCheck className="w-3 h-3" />
              <span>允许并开始</span>
            </button>
          )}
          {!terminal && !needsGrant && (
            <button
              type="button"
              disabled={busy}
              onClick={cancel}
              className="rounded border border-paper-300 px-2 py-1 text-[11px] text-ink-500 hover:text-err disabled:opacity-50"
            >
              取消
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function AgentArtifactLink({
  item,
}: {
  item: { name?: string; url?: string; kind?: string };
}): JSX.Element {
  const rawUrl = typeof item.url === "string" ? item.url : "";
  const [href, setHref] = useState<string | undefined>(
    rawUrl && !rawUrl.startsWith("/") ? rawUrl : undefined,
  );

  useEffect(() => {
    let alive = true;
    if (!rawUrl) {
      setHref(undefined);
      return () => {
        alive = false;
      };
    }
    if (!rawUrl.startsWith("/")) {
      setHref(rawUrl);
      return () => {
        alive = false;
      };
    }
    void apiUrl(rawUrl).then((resolved) => {
      if (alive) setHref(resolved);
    });
    return () => {
      alive = false;
    };
  }, [rawUrl]);

  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="flex items-center gap-1.5 text-[11px] text-accent hover:underline"
    >
      <Download className="w-3 h-3" />
      <span className="truncate">{item.name ?? "产物"}</span>
    </a>
  );
}

interface FailedArtifactCardProps {
  failed: FailedArtifact;
  onDismiss: () => void;
}

/**
 * 失败产物卡片：红色描边 + 错误原因 + 真实重试/关闭。
 */
function FailedArtifactCard({
  failed,
  onDismiss,
}: FailedArtifactCardProps): JSX.Element {
  const [now, setNow] = useState(() => Date.now());
  const [retrying, setRetrying] = useState(false);
  const addArtifact = useStore((s) => s.addArtifact);

  useEffect(() => {
    // 让相对时间每分钟自动刷一次（同一面板里多张卡片共用一个 tick 也 OK）。
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const relative = formatRelativeTime(failed.failed_at, now);
  const typeBadgeClass =
    typeBadge[failed.artifact_type] ?? "bg-paper-200 text-ink-700";
  const canRetry = Boolean(failed.intent_text && failed.artifact_type !== "unknown");

  async function onRetry(e: React.MouseEvent): Promise<void> {
    e.stopPropagation();
    if (!canRetry || retrying || !failed.intent_text) return;
    setRetrying(true);
    try {
      const artifact = await generateArtifact({
        artifact_type: failed.artifact_type as ArtifactKind,
        brief: failed.intent_text,
        meeting_id: failed.meeting_id ?? undefined,
        todo_id: failed.todo_id ?? undefined,
        retry_of_run_id: failed.run_id ?? undefined,
      });
      addArtifact(artifact);
      onDismiss();
      message.success(`已重新生成：${artifact.title || artifact.artifact_id}`);
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      message.error(`重试失败：${text}`);
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div
      data-testid="failed-artifact-card"
      className="border border-err/30 bg-err/5 rounded-md px-3 py-2 space-y-1.5"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[12px] text-err font-medium">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
          <span>生成失败</span>
          <span
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${typeBadgeClass}`}
          >
            {typeIcon[failed.artifact_type] ?? null}
            <span className="uppercase">{failed.artifact_type}</span>
          </span>
        </span>
        <span className="flex items-center gap-2 shrink-0">
          {relative && (
            <span className="text-[11px] text-ink-400" title={failed.failed_at}>
              {relative}
            </span>
          )}
          <button
            type="button"
            aria-label="关闭失败卡片"
            onClick={onDismiss}
            className="text-ink-400 hover:text-ink-700 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </span>
      </div>

      {failed.intent_text && (
        <div
          className="text-[12px] text-ink-700 truncate"
          title={failed.intent_text}
        >
          {failed.intent_text}
        </div>
      )}

      <div
        className="text-[11px] text-ink-500 leading-relaxed overflow-hidden"
        style={{
          display: "-webkit-box",
          WebkitBoxOrient: "vertical",
          WebkitLineClamp: 2,
        }}
        title={failed.reason}
      >
        {failed.reason}
      </div>

      <div className="flex justify-end pt-0.5">
        <button
          type="button"
          onClick={onRetry}
          disabled={!canRetry || retrying}
          title={canRetry ? "重新生成" : "缺少原始指令，无法重试"}
          className="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] text-err border border-err/30 hover:bg-err/10 transition-colors disabled:opacity-45 disabled:cursor-not-allowed"
        >
          <RotateCcw className="w-3 h-3" />
          <span>{retrying ? "重试中" : "重试"}</span>
        </button>
      </div>
    </div>
  );
}

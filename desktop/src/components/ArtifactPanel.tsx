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
  artifactIdFromDownloadHref,
  cancelAgentTask,
  generateArtifact,
  grantAgentRunnerAndResume,
  listAgentTasks,
  listArtifacts,
  type ArtifactKind,
} from "@/api";
import { useStore } from "@/store";
import type { AgentTaskCard, GeneratedArtifact } from "@/types";
import { formatRelativeTime, type FailedArtifact } from "@/lib/failedArtifact";
import ArtifactPreviewModal from "@/components/ArtifactPreviewModal";
import AuthenticatedDownloadLink from "@/components/AuthenticatedDownloadLink";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";

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

const typeLabel: Record<string, string> = {
  word: "文档",
  docx: "文档",
  xlsx: "表格",
  excel: "表格",
  pptx: "演示文稿",
  ppt: "演示文稿",
  html: "网页",
  markdown: "Markdown",
  md: "Markdown",
  pdf: "PDF",
  txt: "文本",
  text: "文本",
};

function artifactFallbackTitle(type: string): string {
  return `未命名${typeLabel[type] ?? "文件"}`;
}

function friendlyFailureReason(reason: string | undefined): string {
  const normalized = reason?.toLocaleLowerCase() ?? "";
  if (normalized.includes("timeout") || normalized.includes("超时")) {
    return "生成时间过长，已停止本次任务。可简化要求后重试。";
  }
  if (
    normalized.includes("no_api_key") ||
    normalized.includes("api key") ||
    normalized.includes("未配置")
  ) {
    return "生成服务尚未配置，请在设置中完成配置后重试。";
  }
  if (
    normalized.includes("network") ||
    normalized.includes("connect") ||
    normalized.includes("http") ||
    normalized.includes("网络")
  ) {
    return "暂时无法连接生成服务，请检查网络后重试。";
  }
  if (normalized.includes("permission") || normalized.includes("权限")) {
    return "任务需要额外权限，请确认授权后重试。";
  }
  return "本次生成未完成。可稍后重试，已有产物不会受影响。";
}

export default function ArtifactPanel(): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
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
    setPreviewArtifact(null);
    Modal.destroyAll();
  }, [backendOriginRevision]);

  useEffect(() => {
    if (!connected) return;
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && isCurrent(originGeneration) && !controller.signal.aborted;
    void (async (): Promise<void> => {
      const [restoredResult, tasksResult] = await Promise.allSettled([
        listArtifacts(500, { signal: controller.signal }),
        listAgentTasks(50, { signal: controller.signal }),
      ]);
      if (!canCommit()) return;
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
      unregisterController();
    };
  }, [
    addArtifact,
    backendOriginRevision,
    captureGeneration,
    connected,
    isCurrent,
    registerAbortController,
    upsertAgentTask,
  ]);

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
    const originGeneration = captureGeneration();
    Modal.confirm({
      title: "清空工作产物",
      content: `确定清空 ${globalArtifacts.length} 条历史产物？该操作不可撤回（文件本身仍保留在磁盘）。`,
      okText: "清空",
      okType: "danger",
      cancelText: "取消",
      onOk: () => {
        if (isCurrent(originGeneration)) clearArtifacts();
      },
    });
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-paper-50">
      <div className="flex items-center justify-between px-6 h-11 border-b border-paper-300 shrink-0">
        <span className="text-[13px] text-ink-700 font-medium">
          工作产物
          <span className="sr-only">outputs</span>
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
              aria-label="清空工作产物"
              onClick={onClearAll}
              className="p-1 rounded text-ink-400 hover:text-err hover:bg-paper-150 focus-visible:text-err focus-visible:bg-paper-150 transition-colors"
              title="清空工作产物"
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
        {visibleFailed.length > 0 && (
          <ArtifactSection title="生成失败" count={visibleFailed.length} tone="danger">
            {visibleFailed.map((f) => (
              <FailedArtifactCard
                key={f.id}
                failed={f}
                onDismiss={() => dismissFailedArtifact(f.id)}
              />
            ))}
          </ArtifactSection>
        )}

        {visibleAgentTasks.length > 0 && (
          <ArtifactSection title="执行任务" count={visibleAgentTasks.length}>
            {visibleAgentTasks.map((task) => (
              <AgentTaskCardView
                key={task.task_id}
                task={task}
                onUpdate={upsertAgentTask}
              />
            ))}
          </ArtifactSection>
        )}

        {artifacts.length === 0 && visibleFailed.length === 0 && visibleAgentTasks.length === 0 && (
          <div className="px-3 py-8 text-center text-ink-400 text-[11px] space-y-1">
            <div>暂无产物</div>
            <div className="text-ink-300">
              描述你需要的文档、表格或演示文稿即可开始生成
            </div>
          </div>
        )}
        {artifacts.length > 0 && (
          <ArtifactSection title="已生成文件" count={artifacts.length}>
            {artifacts.map((a) => {
              const displayName = a.title?.trim() || artifactFallbackTitle(a.artifact_type);
              return (
                <div
                  key={a.artifact_id}
                  data-testid="artifact-card"
                  data-artifact-id={a.artifact_id}
                  className="group px-3 py-2.5 rounded-lg hover:bg-paper-150 focus-visible:bg-paper-150 cursor-pointer transition-colors outline-none"
                  onClick={() => setPreviewArtifact(a)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setPreviewArtifact(a);
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  aria-label={`打开${displayName}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="flex items-start gap-2 min-w-0 flex-1">
                      <span
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0 ${
                          typeBadge[a.artifact_type] ?? "bg-paper-200 text-ink-700"
                        }`}
                      >
                        {typeIcon[a.artifact_type] ?? null}
                        <span>{typeLabel[a.artifact_type] ?? "文件"}</span>
                      </span>
                      <span
                        className="text-[13px] leading-5 text-ink-800 font-medium line-clamp-2 break-words min-w-0"
                        title={displayName}
                        data-testid="artifact-title"
                      >
                        {displayName}
                      </span>
                    </span>
                    <span className="flex items-center gap-1 shrink-0 opacity-70 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                      <AuthenticatedDownloadLink
                        url={artifactDownloadUrl(a.artifact_id)}
                        downloadName={displayName}
                        ariaLabel={`下载${displayName}`}
                        stopPropagation
                        className="p-1 rounded hover:bg-paper-200 focus-visible:bg-paper-200"
                      >
                        <Download className="w-3.5 h-3.5 text-ink-500 hover:text-accent" />
                      </AuthenticatedDownloadLink>
                      <button
                        type="button"
                        data-testid="remove-artifact-btn"
                        aria-label={`从列表移除${displayName}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          removeArtifact(a.artifact_id);
                        }}
                        className="p-1 rounded text-ink-400 hover:text-err hover:bg-paper-200 focus-visible:text-err focus-visible:bg-paper-200"
                        title="从列表移除（不删除文件）"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </span>
                  </div>
                  <div className="text-[11px] text-ink-400 mt-1 pl-1">
                    {(a.size_bytes / 1024).toFixed(1)} KB
                  </div>
                </div>
              );
            })}
          </ArtifactSection>
        )}
      </div>

      <ArtifactPreviewModal
        artifact={previewArtifact}
        onClose={() => setPreviewArtifact(null)}
      />
    </div>
  );
}

function ArtifactSection({
  title,
  count,
  tone = "default",
  children,
}: {
  title: string;
  count: number;
  tone?: "default" | "danger";
  children: React.ReactNode;
}): JSX.Element {
  return (
    <section className="space-y-1.5" aria-label={title}>
      <div
        className={`flex items-center justify-between px-1 pt-1 text-[11px] font-medium ${
          tone === "danger" ? "text-err" : "text-ink-500"
        }`}
      >
        <span>{title}</span>
        <span className="tabular-nums text-ink-400">{count}</span>
      </div>
      <div className="space-y-1">{children}</div>
    </section>
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

function terminalProgressText(state: string): string | null {
  switch (state) {
    case "succeeded":
      return "任务已完成";
    case "failed":
      return "任务执行失败";
    case "cancelled":
      return "任务已取消";
    case "cancel_failed":
      return "任务取消失败";
    case "timeout":
      return "任务执行超时";
    default:
      return null;
  }
}

function AgentTaskCardView({
  task,
  onUpdate,
}: {
  task: AgentTaskCard;
  onUpdate: (task: AgentTaskCard) => void;
}): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
  } = useBackendOriginFence();
  const [busy, setBusy] = useState(false);
  const snapshot = task.snapshot ?? {};
  const artifacts = Array.isArray(snapshot.artifacts) ? snapshot.artifacts : task.artifacts;
  const terminalProgress = terminalProgressText(task.state);
  const terminal = terminalProgress !== null;
  const needsGrant = task.state === "waiting_permission";
  const failed = task.state === "failed" || task.state === "timeout" || task.state === "cancel_failed";
  const cancelled = task.state === "cancelled";
  const Icon = task.state === "succeeded"
    ? CheckCircle2
    : failed
      ? AlertCircle
      : cancelled
        ? X
        : Clock3;
  const progressText = terminalProgress
    ?? (typeof snapshot.progress_text === "string" && snapshot.progress_text.trim().length > 0
      ? snapshot.progress_text.trim()
      : task.progress_text?.trim() || statusLabel(task.state));
  const finalText =
    typeof snapshot.final_text === "string" && snapshot.final_text.trim().length > 0
      ? snapshot.final_text.trim()
      : task.final_text?.trim() ?? "";
  const errorText =
    typeof snapshot.error === "string" && snapshot.error.trim().length > 0
      ? snapshot.error.trim()
      : task.error?.trim() ?? "";

  useEffect(() => {
    setBusy(false);
  }, [backendOriginRevision]);

  async function grantAndStart(e: MouseEvent): Promise<void> {
    e.stopPropagation();
    const originGeneration = captureGeneration();
    setBusy(true);
    try {
      const res = await grantAgentRunnerAndResume(task.task_id);
      if (isCurrent(originGeneration) && res.resumed_task) {
        onUpdate(res.resumed_task);
      }
    } finally {
      if (isCurrent(originGeneration)) setBusy(false);
    }
  }

  async function cancel(e: MouseEvent): Promise<void> {
    e.stopPropagation();
    const originGeneration = captureGeneration();
    setBusy(true);
    try {
      const updated = await cancelAgentTask(task.task_id);
      if (isCurrent(originGeneration)) onUpdate(updated);
    } finally {
      if (isCurrent(originGeneration)) setBusy(false);
    }
  }

  return (
    <div
      data-testid="agent-task-card"
      data-task-id={task.task_id}
      data-task-state={task.state}
      className={`rounded-md border px-3 py-2.5 space-y-2 ${
        failed ? "border-err/30 bg-err/5" : "border-paper-300 bg-white"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex items-center gap-1.5 text-[12px] font-medium text-ink-800">
            <Icon
              className={`w-3.5 h-3.5 ${
                failed ? "text-err" : cancelled ? "text-ink-400" : "text-accent"
              }`}
            />
            <span className="truncate">{task.title || "EchoDesk 正在执行"}</span>
          </div>
          <div
            className="text-[11px] text-ink-500 leading-relaxed break-words [overflow-wrap:anywhere]"
            data-testid="agent-task-progress"
          >
            {progressText}
          </div>
        </div>
        <span
          className="shrink-0 rounded bg-paper-150 px-1.5 py-0.5 text-[10px] text-ink-500"
          data-testid="agent-task-status"
        >
          {statusLabel(task.state)}
        </span>
      </div>

      {finalText.length > 0 && (
        <div className="rounded bg-paper-100 px-2 py-1.5 text-[11px] text-ink-700 leading-relaxed break-words [overflow-wrap:anywhere]">
          {finalText}
        </div>
      )}

      {errorText.length > 0 && (
        <div className="text-[11px] text-err leading-relaxed break-words [overflow-wrap:anywhere]">
          {friendlyFailureReason(errorText)}
        </div>
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
  const artifactId = artifactIdFromDownloadHref(rawUrl);
  const authenticatedUrl = artifactId ? artifactDownloadUrl(artifactId) : rawUrl;

  return (
    <AuthenticatedDownloadLink
      url={authenticatedUrl}
      downloadName={item.name}
      className="flex items-center gap-1.5 text-[11px] text-accent hover:underline"
    >
      <Download className="w-3 h-3" />
      <span className="truncate">{item.name ?? "产物"}</span>
    </AuthenticatedDownloadLink>
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
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
  } = useBackendOriginFence();
  const [now, setNow] = useState(() => Date.now());
  const [retrying, setRetrying] = useState(false);
  const addArtifact = useStore((s) => s.addArtifact);

  useEffect(() => {
    // 让相对时间每分钟自动刷一次（同一面板里多张卡片共用一个 tick 也 OK）。
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    setRetrying(false);
  }, [backendOriginRevision]);

  const relative = formatRelativeTime(failed.failed_at, now);
  const typeBadgeClass =
    typeBadge[failed.artifact_type] ?? "bg-paper-200 text-ink-700";
  const canRetry = Boolean(failed.intent_text && failed.artifact_type !== "unknown");

  async function onRetry(e: React.MouseEvent): Promise<void> {
    e.stopPropagation();
    if (!canRetry || retrying || !failed.intent_text) return;
    const originGeneration = captureGeneration();
    setRetrying(true);
    try {
      const artifact = await generateArtifact({
        artifact_type: failed.artifact_type as ArtifactKind,
        brief: failed.intent_text,
        meeting_id: failed.meeting_id ?? undefined,
        todo_id: failed.todo_id ?? undefined,
        retry_of_run_id: failed.run_id ?? undefined,
      });
      if (isCurrent(originGeneration)) {
        addArtifact(artifact);
        onDismiss();
        message.success(
          `已重新生成：${artifact.title?.trim() || artifactFallbackTitle(artifact.artifact_type)}`,
        );
      }
    } catch (err) {
      if (!isCurrent(originGeneration)) return;
      console.error("[artifact-panel] retry failed", err);
      message.error("重试失败，请稍后再试");
    } finally {
      if (isCurrent(originGeneration)) setRetrying(false);
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
            <span>{typeLabel[failed.artifact_type] ?? "文件"}</span>
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
      >
        {friendlyFailureReason(failed.reason)}
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

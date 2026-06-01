import { useEffect, useState } from "react";
import { Modal, message } from "antd";
import {
  AlertCircle,
  Download,
  FileCode,
  FileText,
  FileType2,
  FileSpreadsheet,
  Globe,
  Presentation,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import {
  artifactDownloadUrl,
  generateArtifactStream,
  listArtifacts,
  type ArtifactKind,
} from "@/api";
import { useStore } from "@/store";
import type { GeneratedArtifact } from "@/types";
import { formatRelativeTime, type FailedArtifact } from "@/lib/failedArtifact";
import ArtifactPreviewModal from "@/components/ArtifactPreviewModal";

/**
 * 产物面板：展示历史产物列表（只读）+ 7 类 in-app 预览。
 *
 * 2026-05 修订（P4.1 M4）：
 * - 全部 7 类（html / pptx / xlsx / word / markdown / pdf / txt）整条都可点击预览
 *   - pptx 走 Electron shell.openPath → Keynote；其他类型在 Modal 内渲染
 * - 顶栏新增「清空产物」按钮（confirm 后清空 store.artifacts；不动失败卡片）
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

const retryableArtifactKinds = new Set<string>([
  "word",
  "docx",
  "xlsx",
  "excel",
  "pptx",
  "ppt",
  "html",
  "markdown",
  "md",
  "mdown",
  "pdf",
  "txt",
  "text",
]);

function toArtifactKind(value: string): ArtifactKind | null {
  return retryableArtifactKinds.has(value) ? (value as ArtifactKind) : null;
}

export default function ArtifactPanel(): JSX.Element {
  const globalArtifacts = useStore((s) => s.artifacts);
  const addArtifact = useStore((s) => s.addArtifact);
  const failedArtifacts = useStore((s) => s.failedArtifacts);
  const dismissFailedArtifact = useStore((s) => s.dismissFailedArtifact);
  const clearArtifacts = useStore((s) => s.clearArtifacts);
  const removeArtifact = useStore((s) => s.removeArtifact);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentMeetingId ? s.meetings[currentMeetingId] : undefined,
  );
  const [previewArtifact, setPreviewArtifact] =
    useState<GeneratedArtifact | null>(null);
  const [historyLoaded, setHistoryLoaded] = useState(false);

  // 启动期加载历史产物，带指数退避重试（与 useMeetingHistory 同模式）。
  // 原先 catch 里 setHistoryLoaded(true) 会让失败后永不重试，导致后端比
  // 前端晚起来时（常见情况）产物列表一直是空。
  useEffect(() => {
    if (historyLoaded || globalArtifacts.length > 0) return;
    let alive = true;
    const delays = [0, 500, 1500, 4000, 8000];
    void (async () => {
      for (let i = 0; i < delays.length && alive; i++) {
        if (delays[i] > 0) {
          await new Promise<void>((res) => setTimeout(res, delays[i]));
        }
        if (!alive) return;
        try {
          const items = await listArtifacts(100);
          if (!alive) return;
          for (const artifact of [...items].reverse()) {
            addArtifact(artifact);
          }
          setHistoryLoaded(true);
          return; // 成功即止
        } catch {
          // 继续下一轮重试，最后一次失败才放弃
          if (i === delays.length - 1 && alive) setHistoryLoaded(true);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [addArtifact, globalArtifacts.length, historyLoaded]);

  // 选中具体会议 → 仅展示该会议的产物（meeting.artifacts 由 ws 事件维护，
  // 详见 store.ts 的 artifact.ready handler）。"伴随时段"（currentMeetingId
  // === null）走全局视图，与历史一致。
  const scopedToMeeting = currentMeetingId !== null && meeting !== undefined;
  const artifacts = scopedToMeeting ? meeting.artifacts : globalArtifacts;
  // 失败卡片仍走全局：它们只活在当前会话，没有 per-meeting 归属
  const showFailed = !scopedToMeeting;
  const visibleFailed = showFailed ? failedArtifacts : [];

  function onClearAll(): void {
    Modal.confirm({
      title: "清空产物列表",
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
        <span className="text-[13px] text-ink-700 font-medium tracking-wider">
          {scopedToMeeting ? "本会议产物" : "全部产物"}
        </span>
        <span className="flex items-center gap-2">
          <span className="text-[11px] text-ink-400">
            {visibleFailed.length > 0
              ? `${artifacts.length} · ${visibleFailed.length} 失败`
              : artifacts.length}
          </span>
          {!scopedToMeeting && globalArtifacts.length > 0 && (
            <button
              type="button"
              data-testid="clear-artifacts-btn"
              aria-label="清空产物列表"
              onClick={onClearAll}
              className="p-1 rounded text-ink-400 hover:text-err hover:bg-paper-150 transition-colors"
              title="清空产物列表"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </span>
      </div>

      <div
        className="flex-1 overflow-y-auto px-3 py-2 space-y-1"
        data-testid="artifact-list"
        data-scope={scopedToMeeting ? "meeting" : "global"}
      >
        {visibleFailed.map((f) => (
          <FailedArtifactCard
            key={f.id}
            failed={f}
            onDismiss={() => dismissFailedArtifact(f.id)}
          />
        ))}
        {artifacts.length === 0 && visibleFailed.length === 0 && (
          <div className="px-3 py-8 text-center text-ink-400 text-[11px] space-y-1">
            <div>{scopedToMeeting ? "该会议暂无产物" : "暂无产物"}</div>
            <div className="text-ink-300">
              {scopedToMeeting
                ? "切到「伴随时段」查看全部历史产物"
                : "在输入框输入 @生成 PPT / @报告 / @Excel … 触发"}
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
                {/* 历史恢复的产物没有真实生成耗时，显示「历史」而非误导的 0.0s */}
                <span>
                  {a.model === "restored" || a.generation_latency_ms <= 0
                    ? "历史"
                    : `${(a.generation_latency_ms / 1000).toFixed(1)}s`}
                </span>
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

interface FailedArtifactCardProps {
  failed: FailedArtifact;
  onDismiss: () => void;
}

/**
 * 失败产物卡片：红色描边 + 错误原因 + 重试/关闭。
 *
 * 重试会复用失败事件里回填的原始 brief，并继续携带 meeting_id/todo_id，
 * 这样来自会议待办的产物失败后仍能回写对应 todo。
 */
function FailedArtifactCard({
  failed,
  onDismiss,
}: FailedArtifactCardProps): JSX.Element {
  const addArtifact = useStore((s) => s.addArtifact);
  const [now, setNow] = useState(() => Date.now());
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    // 让相对时间每分钟自动刷一次（同一面板里多张卡片共用一个 tick 也 OK）。
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  const relative = formatRelativeTime(failed.failed_at, now);
  const typeBadgeClass =
    typeBadge[failed.artifact_type] ?? "bg-paper-200 text-ink-700";

  async function onRetry(e: React.MouseEvent): Promise<void> {
    e.stopPropagation();
    const artifactType = toArtifactKind(failed.artifact_type);
    if (!artifactType || !failed.intent_text) {
      message.error("缺少可重试的原始指令，请重新在输入框发起生成");
      return;
    }
    setRetrying(true);
    let sawDone = false;
    let sawError = false;
    try {
      await generateArtifactStream(
        {
          artifact_type: artifactType,
          brief: failed.intent_text,
          meeting_id: failed.meeting_id ?? undefined,
          todo_id: failed.todo_id ?? undefined,
        },
        {
          onDone: (artifact) => {
            sawDone = true;
            addArtifact(artifact);
            message.success(`已重新生成：${artifact.title || artifact.artifact_id}`);
            onDismiss();
          },
          onError: ({ error }) => {
            sawError = true;
            message.error(`重试失败：${error}`);
          },
        },
      );
      if (!sawDone && !sawError) {
        message.error("重试失败：后端流结束但没有返回产物");
      }
    } catch (err) {
      const raw = err instanceof Error ? err.message : String(err);
      message.error(`重试失败：${raw}`);
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
          disabled={retrying}
          className="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] text-err border border-err/30 hover:bg-err/10 transition-colors"
        >
          <RotateCcw className={`w-3 h-3 ${retrying ? "animate-spin" : ""}`} />
          <span>{retrying ? "重试中" : "重试"}</span>
        </button>
      </div>
    </div>
  );
}

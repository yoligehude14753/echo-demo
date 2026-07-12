import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button, Empty, Tag, Tooltip, message } from "antd";
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  Download,
  FileText,
  Loader2,
  Play,
  QrCode,
  RefreshCw,
} from "lucide-react";
import {
  artifactDownloadUrl,
  generateArtifact,
  getMeetingMinutes,
  listWorkflowRuns,
  retryMinutesGeneration,
  type ArtifactKind,
} from "@/api";
import { buildSpeakerDisplayMap } from "@/lib/speakerDisplay";
import MeetingShareModal from "@/components/MeetingShareModal";
import AuthenticatedDownloadLink from "@/components/AuthenticatedDownloadLink";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { backendBaseSnapshot } from "@/runtime";

// 把 LLM 返回的 assignee（"说话人 18" / "说话人18" / 已被用户改名的 "李雷"）
// remap 成 transcript 视图里同一个 displayIdx，避免 minutes 写"说话人 21"但
// 转写流显示"说话人 3"两边对不上。
// 规则：能解析出 "说话人\s*\d+" 的就走 displayMap.get(rawLabel) → "说话人 N"；
// 解析不出（用户改名 / null）原样返回。
function remapAssignee(
  raw: string | null | undefined,
  displayMap: Map<string, number>,
): string | null {
  if (!raw) return null;
  const m = raw.match(/^说话人\s*(\d+)$/);
  if (!m) return raw; // 用户已改名 / 自定义
  // displayMap 的 key 是 segments 里的 speaker_label，可能是 "说话人 18" 也可能是 "speaker_18"
  // 优先按原字符串查；查不到再按数字部分构造的 key 试一次
  const direct = displayMap.get(raw);
  if (direct !== undefined) return `说话人 ${direct}`;
  // 兜底：整个 displayMap 里有没有 label 末尾数字 = 当前数字的 key
  const num = m[1];
  for (const [label, idx] of displayMap.entries()) {
    if (label.endsWith(num) || label === `说话人 ${num}` || label === `说话人${num}`) {
      return `说话人 ${idx}`;
    }
  }
  return raw; // displayMap 里没这人，原样
}
import { projectMinutesWithWorkflowRuns, useStore } from "@/store";
import type { MeetingMinutes, TodoItem } from "@/types";

function isFinalizedLike(state: string | undefined): boolean {
  return state === "ended" || state === "finalized";
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  const remainingSeconds = total % 60;
  return remainingSeconds > 0
    ? `${minutes} 分 ${remainingSeconds} 秒`
    : `${minutes} 分钟`;
}

function friendlyMinutesError(raw: string | null | undefined): {
  headline: string;
  hint: string;
} {
  const s = (raw ?? "").trim();
  if (!s) {
    return { headline: "未知错误", hint: "请点击重新生成纪要再试一次" };
  }
  if (/JSON parse failed|delimiter|Expecting/i.test(s)) {
    return {
      headline: "纪要格式不规范",
      hint: "智能引擎偶发会返回非标准结构。点击重新生成通常可解决",
    };
  }
  if (/timeout|timed out/i.test(s)) {
    return {
      headline: "生成时间过长",
      hint: "服务当前响应较慢，稍后点击重新生成即可",
    };
  }
  if (/connect refused|connection refused|read timed out/i.test(s)) {
    return {
      headline: "暂时无法连接生成服务",
      hint: "检查网络连接后点击重新生成",
    };
  }
  if (/rate limit|429/i.test(s)) {
    return {
      headline: "当前请求较多",
      hint: "稍等片刻后点击重新生成",
    };
  }
  return {
    headline: "纪要生成失败",
    hint: "点击下方按钮重新生成；如反复失败，请检查网络或服务设置",
  };
}

/**
 * MinutesView · 区分 4 个状态（2026-05-28 修：之前只有「无 / 有」两种）
 *
 *   会议中（state="in_meeting" 且无 minutes）
 *     → 「会议进行中…」
 *   生成中（state="ended" 且 minutes_status=null|"generating"）
 *     → 「正在生成纪要…」+ Spinner
 *   生成失败（state="ended" 且 minutes_status="generation_failed"）
 *     → 「生成失败 · 点击重试」+ 错误消息
 *   已生成（minutes 有内容）
 *     → 渲染纪要详情
 *
 * 解决的 bug：echo-demo backend.log 2026-05-28 10:39 LLM 调用失败后，UI 永远
 * 显示「纪要尚未生成 / 结束会议后由智能引擎自动产出」，用户没有任何重试入口。
 */
export default function MinutesView(): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const currentId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentId ? s.meetings[currentId] : undefined,
  );
  const upsertMeeting = useStore((s) => s.upsertMeeting);
  const removeArtifact = useStore((s) => s.removeArtifact);
  const [retrying, setRetrying] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);

  // 切换 meeting 时若 store 没缓存 minutes 但后端已 finalized → 主动 fetch
  // 修 bug：sqlite 里 m-7ffe56cc4ad8 state="finalized" minutes_json=YES，
  // 但 store 只在 minutes.ready ws 事件时填充 minutes；用户事后从左侧列表
  // 切回老会议时 store 没拿到 minutes，永远卡在「正在生成…」假象。
  const fetchedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    fetchedRef.current.clear();
    setRetrying(false);
    setShareOpen(false);
  }, [backendOriginRevision]);

  useEffect(() => {
    if (!currentId) return;
    if (meeting?.minutes) return;
    if (meeting?.minutes_status === "generation_failed") return;
    if (!isFinalizedLike(meeting?.state)) return;
    if (fetchedRef.current.has(currentId)) return;
    fetchedRef.current.add(currentId);
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && isCurrent(originGeneration) && !controller.signal.aborted;
    Promise.all([
      getMeetingMinutes(currentId, { signal: controller.signal }),
      listWorkflowRuns(
        { meeting_id: currentId, limit: 100 },
        { signal: controller.signal },
      ).catch(() => []),
    ])
      .then(([m, workflowRuns]) => {
        if (!canCommit()) return;
        if (!m) return; // 真的 404 → 让 generating 状态自然展示
        const restoredMinutes = projectMinutesWithWorkflowRuns(m, workflowRuns);
        if (!restoredMinutes) return;
        upsertMeeting(currentId, {
          minutes: restoredMinutes,
          title: restoredMinutes.title,
          state: "ended",
          minutes_status: "ok",
          minutes_error: null,
        });
      })
      .catch(() => {
        if (!canCommit()) return;
        // 网络错不能永久占用 fetchedRef；后续重新选择或状态变化必须可重试。
        fetchedRef.current.delete(currentId);
      });
    return () => {
      alive = false;
      unregisterController();
    };
  }, [
    backendOriginRevision,
    captureGeneration,
    currentId,
    isCurrent,
    meeting?.minutes,
    meeting?.minutes_status,
    meeting?.state,
    registerAbortController,
    upsertMeeting,
  ]);

  const onRetry = async (): Promise<void> => {
    if (!currentId || !meeting) return;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    setRetrying(true);
    try {
      await retryMinutesGeneration(currentId, meeting.title || currentId, {
        signal: controller.signal,
      });
      if (!isCurrent(originGeneration) || controller.signal.aborted) return;
      message.success("已重新提交，等待 LLM 返回…");
    } catch (e) {
      if (!isCurrent(originGeneration) || controller.signal.aborted) return;
      console.error("[minutes] retry failed", e);
      message.error("重试失败，请稍后再试");
    } finally {
      unregisterController();
      if (isCurrent(originGeneration)) setRetrying(false);
    }
  };

  const shareAction =
    currentId && meeting && meeting.state !== "in_meeting" ? (
      <Button
        size="small"
        type="default"
        icon={<QrCode className="w-3 h-3" />}
        onClick={() => setShareOpen(true)}
        data-testid="open-meeting-share"
        className="!text-accent"
      >
        扫码保存
      </Button>
    ) : null;

  const shareModal = (
    <MeetingShareModal
      open={shareOpen}
      meeting={meeting}
      onClose={() => setShareOpen(false)}
      onOutputsCleared={(artifactIds) => {
        artifactIds.forEach((id) => removeArtifact(id));
        if (currentId) {
          upsertMeeting(currentId, {
            minutes: undefined,
            minutes_status: null,
            minutes_error: null,
            display_title: null,
            state: "idle",
            artifacts: [],
          });
        }
      }}
    />
  );

  // 1) 已生成：渲染纪要主体
  if (meeting?.minutes) {
    return (
      <>
        <MinutesBody m={meeting.minutes} shareAction={shareAction} />
        {shareModal}
      </>
    );
  }

  // 2) 失败：给重试按钮 + 错误消息
  if (meeting?.minutes_status === "generation_failed") {
    return (
      <>
        <MinutesErrorCard
          rawError={meeting.minutes_error}
          retrying={retrying}
          onRetry={onRetry}
          shareAction={shareAction}
        />
        {shareModal}
      </>
    );
  }

  // 3) 生成中 / 已 finalized 但 minutes 还没拿到：大转圈 + elapsed
  if (isFinalizedLike(meeting?.state)) {
    return (
      <>
        <MinutesGeneratingCard endedAt={meeting?.ended_at} shareAction={shareAction} />
        {shareModal}
      </>
    );
  }

  // 4) 会议中（in_meeting）或没有任何 meeting
  const inMeeting = meeting?.state === "in_meeting";
  return (
    <div className="px-6 py-6 border-b border-paper-300">
      <div className="flex items-center gap-2 mb-4 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
      </div>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <span className="text-ink-400 text-[11px]">
            {inMeeting ? (
              <>
                会议进行中…
                <br />
                结束会议后会自动生成
              </>
            ) : (
              <>
                纪要尚未生成
                <br />
                开始并结束会议后，纪要会自动出现在这里
              </>
            )}
          </span>
        }
      />
    </div>
  );
}

function MinutesGeneratingCard({
  endedAt,
  shareAction,
}: {
  endedAt?: string | null;
  shareAction?: ReactNode;
}): JSX.Element {
  // elapsed 秒数计时（从会议结束时间开始；没 endedAt 就从挂载时间）
  const start = endedAt ? Date.parse(endedAt) : Date.now();
  const [elapsed, setElapsed] = useState(
    Math.max(0, Math.round((Date.now() - start) / 1000)),
  );
  useEffect(() => {
    const t = setInterval(() => {
      setElapsed(Math.max(0, Math.round((Date.now() - start) / 1000)));
    }, 1000);
    return () => clearInterval(t);
  }, [start]);

  // 阶段文案随时间渐变：让用户感受到"还在跑"，不是死了
  let stage = "正在准备转写素材…";
  if (elapsed > 5) stage = "正在抽取会议要点…";
  if (elapsed > 30) stage = "正在整理决议与待办…";
  if (elapsed > 60) stage = "仍在整理，长会议通常需要 1–2 分钟…";
  if (elapsed > 150) stage = "比预期久一些，会继续在后台处理…";

  return (
    <div className="px-6 py-8 border-b border-paper-300">
      <div className="flex items-center gap-2 mb-6 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
        {shareAction && <span className="ml-auto">{shareAction}</span>}
      </div>
      <div
        className="flex flex-col items-center justify-center py-8"
        data-testid="minutes-generating"
        role="status"
        aria-live="polite"
      >
        <Loader2
          className="w-14 h-14 text-accent/80 animate-spin mb-4"
          strokeWidth={1.5}
        />
        <div className="text-[13.5px] font-medium text-ink-800 mb-1.5">
          会议纪要生成中
        </div>
        <div className="text-[12px] text-ink-500 leading-5 mb-2 text-center max-w-[280px]">
          {stage}
        </div>
        <div className="text-[11px] tabular-nums text-ink-400">
          已等待 {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}
        </div>
      </div>
    </div>
  );
}

function MinutesErrorCard({
  rawError,
  retrying,
  onRetry,
  shareAction,
}: {
  rawError: string | null | undefined;
  retrying: boolean;
  onRetry: () => void;
  shareAction?: ReactNode;
}): JSX.Element {
  const { headline, hint } = friendlyMinutesError(rawError);
  return (
    <div className="px-6 py-6 border-b border-paper-300">
      <div className="flex items-center gap-2 mb-4 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
        {shareAction && <span className="ml-auto">{shareAction}</span>}
      </div>
      <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-3">
        <div className="flex items-start gap-2 mb-3">
          <AlertCircle className="w-4 h-4 text-rose-600 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0 text-rose-800 leading-5">
            <div
              className="text-[13px] font-medium mb-0.5"
              data-testid="minutes-error-headline"
            >
              {headline}
            </div>
            <div className="text-[12px] text-rose-700/85 leading-5 break-words">
              {hint}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 mb-2">
          <Button
            type="primary"
            danger
            size="small"
            icon={<RefreshCw className="w-3 h-3" />}
            loading={retrying}
            onClick={onRetry}
            data-testid="minutes-retry-btn"
          >
            重新生成纪要
          </Button>
        </div>
      </div>
    </div>
  );
}

function MinutesBody({
  m,
  shareAction,
}: {
  m: NonNullable<
    ReturnType<typeof useStore.getState>["meetings"][string]
  >["minutes"];
  shareAction?: ReactNode;
}): JSX.Element {
  if (!m) return <></>;
  // M_minutes_refactor：
  // - 去掉「说话人列表」（用户反馈无价值），只保留标题 + 时长
  // - action_items → todos：渲染为可勾选 TodoList，actionable 给「执行」按钮，
  //   done 划掉 + 显示 artifact 下载链接。
  // - 旧后端没返 todos → 兜底从 action_items 投影成 info 待办，避免行动项消失
  const todos: TodoItem[] = m.todos?.length
    ? m.todos
    : (m.action_items ?? []).map((text, i) => ({
        id: `legacy-${i}`,
        text,
        assignee: null,
        kind: "info" as const,
        status: "pending" as const,
        done_at: null,
        artifact_id: null,
        suggested_command: null,
      }));
  return (
    <div className="px-6 py-5 border-b border-paper-300 max-h-[55vh] overflow-y-auto">
      <div className="flex items-center gap-2 mb-3 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
        {shareAction && <span className="ml-auto">{shareAction}</span>}
      </div>
      <h2
        className="brand text-[17px] font-semibold text-ink-900 leading-snug mb-1"
        data-testid="minutes-title"
      >
        {m.title}
      </h2>
      <div className="text-[11px] text-ink-400 mb-4 flex items-center gap-1.5">
        <span>时长 {formatDuration(m.duration_sec)}</span>
      </div>

      <p className="text-[13.5px] text-ink-800 leading-7 mb-5 break-words [overflow-wrap:anywhere]">{m.summary}</p>

      {m.sections.map((sec, i) => (
        <section key={i} className="mb-4">
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            {sec.heading}
          </h3>
          <ul className="space-y-1 text-[13px] text-ink-700">
            {sec.bullets.map((b, j) => (
              <li key={j} className="flex gap-2 leading-6">
                <span className="text-ink-400 shrink-0">·</span>
                <span className="min-w-0 break-words [overflow-wrap:anywhere]">{b}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}

      {m.decisions.length > 0 && (
        <section className="mb-4">
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            决议
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {m.decisions.map((d, i) => (
              <span
                key={i}
                className="max-w-full break-words [overflow-wrap:anywhere] text-[12px] px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-200"
              >
                {d}
              </span>
            ))}
          </div>
        </section>
      )}

      <MinutesTodoList meetingId={m.meeting_id} minutes={m} todos={todos} />
    </div>
  );
}

function useMeetingSpeakerMap(meetingId: string): Map<string, number> {
  const segs = useStore((s) => s.meetings[meetingId]?.segments ?? []);
  return useMemo(() => buildSpeakerDisplayMap(segs), [segs]);
}

type AutoExecutableTodo = {
  artifact_type: ArtifactKind;
  brief: string;
  extra_instructions: string;
};

function inferAutoExecutableTodo(
  todo: TodoItem,
  minutes: MeetingMinutes,
): AutoExecutableTodo | null {
  if (todo.status !== "pending" || todo.kind !== "actionable") return null;
  const source = `${todo.suggested_command ?? ""}\n${todo.text}`.trim();
  if (!source) return null;
  const lower = source.toLowerCase();
  let artifactType: ArtifactKind | null = null;
  if (/pptx?|幻灯|演示|路演/.test(lower)) artifactType = "pptx";
  else if (/xlsx?|excel|表格|统计表|清单表/.test(lower)) artifactType = "xlsx";
  else if (/html|网页|页面|单页|landing/.test(lower)) artifactType = "html";
  else if (/markdown|md\b|要点|清单|纪要|总结/.test(lower)) artifactType = "markdown";
  else if (/pdf/.test(lower)) artifactType = "pdf";
  else if (/txt|文本/.test(lower)) artifactType = "txt";
  else if (/word|docx|文档|报告|简报|方案|请示|通知/.test(lower)) artifactType = "word";
  if (!artifactType) return null;

  const sectionText = minutes.sections
    .slice(0, 6)
    .map((section) => `【${section.heading}】\n${section.bullets.slice(0, 5).join("\n")}`)
    .join("\n\n");
  const decisions = minutes.decisions.length
    ? `会议决议：\n${minutes.decisions.join("\n")}`
    : "会议决议：无";
  const brief = todo.suggested_command?.trim() || `基于会议纪要执行待办：${todo.text}`;
  const extraInstructions = [
    "这是 EchoDesk 从会议纪要中识别出的可自动执行待办。",
    "仅执行文档/表格/页面/演示稿等 EchoDesk 可完成的生成类任务；不要声称已完成外部联系、线下沟通、付款、审批等外部动作。",
    `会议标题：${minutes.title}`,
    `会议摘要：${minutes.summary}`,
    decisions,
    sectionText ? `会议要点：\n${sectionText}` : "会议要点：无",
    `待办原文：${todo.text}`,
    todo.assignee ? `负责人：${todo.assignee}` : "负责人：未指定",
  ].join("\n\n");
  return {
    artifact_type: artifactType,
    brief,
    extra_instructions: extraInstructions,
  };
}

function autoExecStorageKey(meetingId: string, todoId: string): string {
  const backendScope =
    typeof window === "undefined"
      ? "server"
      : backendBaseSnapshot() || window.location.origin;
  return `echodesk:auto-exec:v1:${encodeURIComponent(backendScope)}:${meetingId}:${todoId}`;
}

function wasAutoExecAttempted(meetingId: string, todoId: string): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(autoExecStorageKey(meetingId, todoId)) === "1";
}

function markAutoExecAttempted(meetingId: string, todoId: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(autoExecStorageKey(meetingId, todoId), "1");
}

/**
 * 会议待办清单。
 *
 * 状态集（19-quality-detail.mdc §状态集完整性）：
 * - empty：todos == [] → 「本次会议无待办」
 * - pending + actionable → ▶️ 执行 按钮（预填 suggested_command 到 CommandBar）
 * - pending + info → 纯文字，无执行按钮
 * - done → 文字划掉 + 🔗 已生成（→ 下载 artifact）
 * - cancelled → 灰色 + 删除线（占位，当前后端尚不会自动置 cancelled）
 *
 * 注意：执行按钮把 todo 文本拼成一条带 todo_id/meeting_id 的指令塞给 CommandBar，
 * CommandBar 发 artifact 时把这两个字段带回后端，后端生成后回写 minutes_json.todos
 * 并发 ``meeting.todo.completed`` 事件 → store.applyEvent 把 status 置 done。
 */
function MinutesTodoList({
  meetingId,
  minutes,
  todos,
}: {
  meetingId: string;
  minutes: MeetingMinutes;
  todos: TodoItem[];
}): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const prefillCommandBar = useStore((s) => s.prefillCommandBar);
  const addArtifact = useStore((s) => s.addArtifact);
  const speakerMap = useMeetingSpeakerMap(meetingId);
  const inFlightRef = useRef<Set<string>>(new Set());
  const [autoRunning, setAutoRunning] = useState<Record<string, boolean>>({});

  useEffect(() => {
    inFlightRef.current.clear();
    setAutoRunning({});
  }, [backendOriginRevision]);

  useEffect(() => {
    setAutoRunning((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const todoId of Object.keys(next)) {
        const todo = todos.find((item) => item.id === todoId);
        if (!todo || !["pending", "running"].includes(todo.status)) {
          delete next[todoId];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [todos]);

  useEffect(() => {
    const candidates = todos
      .map((todo) => ({ todo, job: inferAutoExecutableTodo(todo, minutes) }))
      .filter(
        (item): item is { todo: TodoItem; job: AutoExecutableTodo } =>
          item.job !== null &&
          !wasAutoExecAttempted(meetingId, item.todo.id) &&
          !inFlightRef.current.has(item.todo.id),
      )
      .slice(0, 3);
    if (candidates.length === 0) return;

    let cancelled = false;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      !cancelled &&
      isCurrent(originGeneration) &&
      !controller.signal.aborted;
    void (async () => {
      try {
        for (const { todo, job } of candidates) {
          if (!canCommit()) return;
          inFlightRef.current.add(todo.id);
          markAutoExecAttempted(meetingId, todo.id);
          setAutoRunning((prev) => ({ ...prev, [todo.id]: true }));
          try {
            const artifact = await generateArtifact(
              {
                artifact_type: job.artifact_type,
                brief: job.brief,
                extra_instructions: job.extra_instructions,
                meeting_id: meetingId,
                todo_id: todo.id,
              },
              { signal: controller.signal },
            );
            if (!canCommit()) return;
            addArtifact(artifact);
            message.success(
              `已完成会议待办：${artifact.title?.trim() || "未命名工作产物"}`,
            );
          } catch (e) {
            if (!canCommit()) return;
            console.error("[minutes] automatic todo failed", e);
            message.error("会议待办执行失败，可在纪要中重试");
          } finally {
            inFlightRef.current.delete(todo.id);
            if (canCommit()) {
              setAutoRunning((prev) => {
                const next = { ...prev };
                delete next[todo.id];
                return next;
              });
            }
          }
        }
      } finally {
        unregisterController();
      }
    })();

    return () => {
      cancelled = true;
      unregisterController();
    };
  }, [
    addArtifact,
    backendOriginRevision,
    captureGeneration,
    isCurrent,
    meetingId,
    minutes,
    registerAbortController,
    todos,
  ]);

  if (todos.length === 0) {
    return (
      <section data-testid="minutes-todos-empty">
        <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
          待办
        </h3>
        <div className="text-[12px] text-ink-400 pl-2 border-l-2 border-paper-300 leading-6">
          本次会议无待办
        </div>
      </section>
    );
  }

  return (
    <section data-testid="minutes-todos">
      <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">待办</h3>
      <ul className="space-y-1">
        {todos.map((t) => (
          <TodoRow
            key={t.id}
            todo={t}
            displayAssignee={remapAssignee(t.assignee, speakerMap)}
            autoRunning={autoRunning[t.id] === true}
            onExecute={(text, retryOfRunId) =>
              prefillCommandBar(text, {
                meeting_id: meetingId,
                todo_id: t.id,
                retry_of_run_id: retryOfRunId,
              })
            }
          />
        ))}
      </ul>
    </section>
  );
}

function TodoRow({
  todo,
  displayAssignee,
  autoRunning,
  onExecute,
}: {
  todo: TodoItem;
  displayAssignee: string | null;
  autoRunning: boolean;
  onExecute: (text: string, retryOfRunId?: string) => void;
}): JSX.Element {
  const done = todo.status === "done";
  const cancelled = todo.status === "cancelled";
  const failed = todo.status === "failed";
  const waitingPermission = todo.status === "waiting_permission";
  const running = autoRunning || todo.status === "running";
  const canExecute =
    (todo.status === "pending" || failed) &&
    todo.kind === "actionable" &&
    typeof todo.suggested_command === "string" &&
    todo.suggested_command.length > 0;
  return (
    <li
      data-testid="minutes-todo-row"
      data-todo-id={todo.id}
      data-todo-status={todo.status}
      data-workflow-run-id={todo.workflow_run_id ?? ""}
      className={`flex items-start gap-2 px-2 py-1.5 rounded-md border border-paper-200 bg-paper-50 ${
        done ? "opacity-70" : ""
      }`}
    >
      <div className="mt-0.5 shrink-0">
        {done ? (
          <CheckCircle2
            className="w-4 h-4 text-emerald-600"
            aria-label="已完成"
          />
        ) : failed ? (
          <AlertCircle className="w-4 h-4 text-err" aria-label="执行失败" />
        ) : running ? (
          <Loader2 className="w-4 h-4 text-accent animate-spin" aria-label="执行中" />
        ) : (
          <Circle
            className={`w-4 h-4 ${cancelled ? "text-ink-300" : "text-ink-400"}`}
            aria-label={cancelled ? "已取消" : waitingPermission ? "等待授权" : "待办"}
          />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div
          className={`text-[13px] leading-5 text-ink-800 break-words [overflow-wrap:anywhere] ${
            done || cancelled ? "line-through text-ink-500" : ""
          }`}
        >
          {todo.text}
        </div>
        <div className="mt-0.5 flex items-center flex-wrap gap-1.5 text-[11px] text-ink-500">
          {displayAssignee && (
            <Tag
              color="default"
              className="!m-0 !text-[10.5px] !leading-4 !py-0 !px-1.5"
            >
              {displayAssignee}
            </Tag>
          )}
          {todo.kind === "actionable" && !done && (
            <span
              className={`text-[10.5px] ${
                failed
                  ? "text-err"
                  : waitingPermission
                    ? "text-amber-700"
                    : "text-accent"
              }`}
            >
              {failed
                ? "失败，可重试"
                : waitingPermission
                  ? "等待授权"
                  : running
                    ? "执行中"
                    : "可执行"}
            </span>
          )}
          {done && todo.artifact_id && (
            <AuthenticatedDownloadLink
              url={artifactDownloadUrl(todo.artifact_id)}
              testId="minutes-todo-artifact-link"
              className="inline-flex items-center gap-1 text-emerald-700 hover:text-emerald-800 underline-offset-2 hover:underline"
            >
              <Download className="w-3 h-3" />
              已生成 · 下载
            </AuthenticatedDownloadLink>
          )}
        </div>
      </div>
      {canExecute && (
        <Tooltip title="放入下方输入框，可在发送前修改">
          <Button
            type="default"
            size="small"
            icon={<Play className="w-3 h-3" />}
            data-testid="minutes-todo-execute-btn"
            loading={running}
            disabled={running || waitingPermission}
            onClick={() =>
              onExecute(
                todo.suggested_command as string,
                failed ? (todo.workflow_run_id ?? undefined) : undefined,
              )
            }
            className="!shrink-0 !text-accent"
          >
            {running ? "执行中" : failed ? "重试" : "执行"}
          </Button>
        </Tooltip>
      )}
    </li>
  );
}

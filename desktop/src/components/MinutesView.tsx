import { useState } from "react";
import { Button, Empty, Spin, message } from "antd";
import { AlertCircle, ChevronDown, ChevronRight, FileText, RefreshCw } from "lucide-react";
import { retryMinutesGeneration } from "@/api";
import { useStore } from "@/store";

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
      headline: "LLM 输出格式不规范",
      hint: "MiniMax-M2.7 偶发会返回非标准 JSON。点击重新生成通常可解决",
    };
  }
  if (/timeout|timed out/i.test(s)) {
    return {
      headline: "LLM 调用超时",
      hint: "云端模型当前响应较慢，稍后点击重新生成即可",
    };
  }
  if (/connect refused|connection refused|read timed out/i.test(s)) {
    return {
      headline: "连不上模型服务",
      hint: "检查 Tailscale / 网络后点击重新生成",
    };
  }
  if (/rate limit|429/i.test(s)) {
    return {
      headline: "触发了模型限流",
      hint: "稍等片刻后点击重新生成",
    };
  }
  return { headline: "纪要生成失败", hint: "点击下方按钮重新生成；如反复失败请展开详情排查" };
}

/**
 * MinutesView · 区分 4 个状态（2026-05-28 修：之前只有「无 / 有」两种）
 *
 *   会议中（state="in_meeting" 且无 minutes）
 *     → 「会议进行中…」
 *   生成中（state="ended" 且 minutes_status=null|"generating"）
 *     → 「正在用 MiniMax-M2.7 生成纪要…」+ Spinner
 *   生成失败（state="ended" 且 minutes_status="generation_failed"）
 *     → 「生成失败 · 点击重试」+ 错误消息
 *   已生成（minutes 有内容）
 *     → 渲染纪要详情
 *
 * 解决的 bug：echo-demo backend.log 2026-05-28 10:39 LLM 调用失败后，UI 永远
 * 显示「纪要尚未生成 / 结束会议后由 MiniMax-M2.7 自动产出」，用户没有任何重试入口。
 */
export default function MinutesView(): JSX.Element {
  const currentId = useStore((s) => s.currentMeetingId);
  const meeting = useStore((s) =>
    currentId ? s.meetings[currentId] : undefined,
  );
  const [retrying, setRetrying] = useState(false);

  const onRetry = async (): Promise<void> => {
    if (!currentId || !meeting) return;
    setRetrying(true);
    try {
      await retryMinutesGeneration(currentId, meeting.title || currentId);
      message.success("已重新提交，等待 LLM 返回…");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`重试失败：${msg}`);
    } finally {
      setRetrying(false);
    }
  };

  // 1) 已生成：渲染纪要主体
  if (meeting?.minutes) {
    return <MinutesBody m={meeting.minutes} />;
  }

  // 2) 失败：给重试按钮 + 错误消息
  if (meeting?.minutes_status === "generation_failed") {
    return (
      <MinutesErrorCard
        rawError={meeting.minutes_error}
        retrying={retrying}
        onRetry={onRetry}
      />
    );
  }

  // 3) 生成中：会议已结束、minutes_status 为 generating 或 null（事件还没到）
  if (meeting?.state === "ended") {
    return (
      <div className="px-6 py-6 border-b border-paper-300">
        <div className="flex items-center gap-2 mb-4 text-[13px] text-ink-700 font-medium">
          <FileText className="w-3.5 h-3.5 text-ink-500" />
          <span>会议纪要</span>
        </div>
        <div
          className="flex items-center gap-2 text-[12px] text-ink-500"
          data-testid="minutes-generating"
        >
          <Spin size="small" />
          <span>正在用 MiniMax-M2.7 生成纪要…</span>
        </div>
      </div>
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
                结束会议后由 MiniMax-M2.7 自动产出
              </>
            ) : (
              <>
                纪要尚未生成
                <br />
                结束会议后由 MiniMax-M2.7 自动产出
              </>
            )}
          </span>
        }
      />
    </div>
  );
}

function MinutesErrorCard({
  rawError,
  retrying,
  onRetry,
}: {
  rawError: string | null | undefined;
  retrying: boolean;
  onRetry: () => void;
}): JSX.Element {
  const [showDetail, setShowDetail] = useState(false);
  const { headline, hint } = friendlyMinutesError(rawError);
  const hasDetail = Boolean((rawError ?? "").trim());
  return (
    <div className="px-6 py-6 border-b border-paper-300">
      <div className="flex items-center gap-2 mb-4 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
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
          {hasDetail && (
            <Button
              type="text"
              size="small"
              className="!text-rose-600 hover:!bg-rose-100/60 !px-2 inline-flex items-center"
              onClick={() => setShowDetail((v) => !v)}
              data-testid="minutes-error-toggle"
            >
              {showDetail ? (
                <ChevronDown className="w-3 h-3 mr-0.5" />
              ) : (
                <ChevronRight className="w-3 h-3 mr-0.5" />
              )}
              {showDetail ? "收起详情" : "查看详情"}
            </Button>
          )}
        </div>
        {showDetail && hasDetail && (
          <pre
            className="mt-2 max-h-32 overflow-auto rounded bg-rose-100/70 border border-rose-200 px-2 py-1.5 text-[11px] text-rose-900/80 leading-4 whitespace-pre-wrap break-words font-mono"
            data-testid="minutes-error-detail"
          >
            {rawError}
          </pre>
        )}
      </div>
    </div>
  );
}

function MinutesBody({
  m,
}: {
  m: NonNullable<
    ReturnType<typeof useStore.getState>["meetings"][string]
  >["minutes"];
}): JSX.Element {
  if (!m) return <></>;
  return (
    <div className="px-6 py-5 border-b border-paper-300 max-h-[55vh] overflow-y-auto">
      <div className="flex items-center gap-2 mb-3 text-[13px] text-ink-700 font-medium">
        <FileText className="w-3.5 h-3.5 text-ink-500" />
        <span>会议纪要</span>
      </div>
      <h2 className="brand text-[17px] font-semibold text-ink-900 leading-snug mb-1">
        {m.title}
      </h2>
      <div className="text-[11px] text-ink-400 mb-4 flex items-center gap-1.5">
        <span>时长 {Math.round(m.duration_sec)}s</span>
        <span>·</span>
        <span>{m.speakers.join(" / ")}</span>
      </div>

      <p className="text-[13.5px] text-ink-800 leading-7 mb-5">{m.summary}</p>

      {m.sections.map((sec, i) => (
        <section key={i} className="mb-4">
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            {sec.heading}
          </h3>
          <ul className="space-y-1 text-[13px] text-ink-700">
            {sec.bullets.map((b, j) => (
              <li key={j} className="flex gap-2 leading-6">
                <span className="text-ink-400 shrink-0">·</span>
                <span>{b}</span>
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
                className="text-[12px] px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-200"
              >
                {d}
              </span>
            ))}
          </div>
        </section>
      )}

      {m.action_items.length > 0 && (
        <section>
          <h3 className="text-[12.5px] font-semibold text-ink-900 mb-1.5">
            行动项
          </h3>
          <ul className="space-y-1 text-[13px] text-ink-700">
            {m.action_items.map((a, i) => (
              <li
                key={i}
                className="flex gap-2 leading-6 pl-2 border-l-2 border-paper-300"
              >
                <span>{a}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

import { Layout, Tooltip } from "antd";
import {
  AlertTriangle,
  MessageSquare,
  Mic,
  Settings,
  Square,
  Sparkles,
  Volume2,
  VolumeX,
} from "lucide-react";
import { message } from "antd";
import { getDailyRecap } from "@/api";
import { useState } from "react";
import MeetingList from "@/components/MeetingList";
import TranscriptStream from "@/components/TranscriptStream";
import ArtifactPanel from "@/components/ArtifactPanel";
import MinutesView from "@/components/MinutesView";
import CommandBar from "@/components/CommandBar";
import MeetingStatusBar from "@/components/MeetingStatusBar";
import WorkspaceBar from "@/components/WorkspaceBar";
import StatusBar from "@/components/StatusBar";
import SettingsPanel from "@/components/SettingsPanel";
import OnboardingModal from "@/components/OnboardingModal";
import AboutModal from "@/components/AboutModal";
import { useEchoCapture } from "@/capture/useEchoCapture";
import { useStore } from "@/store";
import { useEchoWS } from "@/ws";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";
import { useVoiceWakeAgent } from "@/hooks/useVoiceWakeAgent";
import { useOnboarding } from "@/hooks/useOnboarding";
import { useMeetingHistory } from "@/hooks/useMeetingHistory";

const { Header, Sider, Content } = Layout;

export default function App(): JSX.Element {
  useEchoWS();
  useMeetingHistory();
  const tts = useTtsPlayer();
  const voiceWake = useVoiceWakeAgent({ tts });
  // 副作用保留：STT 熔断订阅 + /capture/stats 轮询；不再渲染 CaptureStatus chip。
  useEchoCapture({
    onAmbientText: voiceWake.handleAmbientText,
    onEndpoint: voiceWake.handleEndpoint,
  });
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const events = useStore((s) => s.events);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const onboarding = useOnboarding();

  return (
    <Layout className="!h-screen !bg-paper-50 !overflow-hidden">
      <Header className="app-drag flex items-center justify-between !bg-paper-50 !px-5 !h-12 border-b border-paper-300 shrink-0">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full bg-accent shadow-[0_0_0_3px_rgba(16,163,127,0.18)]" />
          <span className="brand font-semibold text-[15px] text-ink-900">
            EchoDesk
          </span>
          <Tooltip title="关于 / 版本">
            <button
              type="button"
              onClick={() => setAboutOpen(true)}
              className="app-no-drag text-[11px] text-ink-500 hover:text-accent transition cursor-pointer"
              data-testid="open-about"
              aria-label="关于 EchoDesk"
            >
              v{__APP_VERSION__}
            </button>
          </Tooltip>
        </div>
        <div className="app-no-drag flex items-center gap-3 text-[11px] text-ink-500">
          <StatusBar
            ttsHealth={tts.synthHealth}
            ttsEnabled={tts.enabled}
            ttsLastError={tts.lastError}
            onRefreshTtsHealth={tts.refreshHealth}
          />
          <span className="w-px h-3 bg-paper-300" aria-hidden />
          <MeetingStatusBar />
          <DailyRecapButton tts={tts} />
          <StopButton tts={tts} />
          <TtsTopBarButton tts={tts} />
          <Tooltip title="后台事件流同步计数">
            <span>同步 {events.length}</span>
          </Tooltip>
          <span className="flex items-center gap-1.5">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                connected ? "bg-accent" : "bg-err"
              }`}
            />
            {connected ? "已连接" : "断线"}
          </span>
          <Tooltip title="设置">
            <button
              type="button"
              onClick={() => setSettingsOpen(true)}
              className="flex items-center rounded px-1.5 py-0.5 text-ink-500 hover:text-ink-700 hover:bg-paper-200 transition"
              data-testid="open-settings"
              aria-label="打开设置"
            >
              <Settings className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        </div>
      </Header>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onReplayOnboarding={onboarding.resetForDebug}
      />

      <OnboardingModal
        open={onboarding.shouldShow}
        onClose={onboarding.markCompleted}
      />

      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />

      <WorkspaceBar />

      <Layout className="!bg-paper-50 !flex-1 !min-h-0 !overflow-hidden">
        <Sider
          width={260}
          className="!bg-paper-150 border-r border-paper-300 !px-2 !py-3 !overflow-y-auto"
        >
          <div className="flex items-center gap-1.5 px-2 mb-2 text-ink-500 text-[11px] uppercase tracking-wider">
            <MessageSquare className="w-3 h-3" />
            <span>会议</span>
          </div>
          <MeetingList />
        </Sider>

        <Content className="flex !bg-paper-50 !min-h-0 !overflow-hidden">
          <div className="flex-1 min-w-0 min-h-0 border-r border-paper-300 flex flex-col">
            <div className="flex items-center gap-2 px-6 h-11 border-b border-paper-300 shrink-0">
              <Mic className="w-3.5 h-3.5 text-ink-500" />
              <span className="text-[13px] text-ink-700 font-medium">
                转写流
              </span>
              {currentMeetingId && (
                <span className="ml-2 text-[11px] text-ink-400">
                  当前会议
                </span>
              )}
            </div>
            <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
              <TranscriptStream />
            </div>
            <div className="shrink-0">
              <CommandBar tts={tts} />
            </div>
          </div>

          <div className="w-[440px] shrink-0 min-h-0 flex flex-col bg-paper-50 overflow-hidden">
            <MinutesView />
            <ArtifactPanel />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

// ── 顶栏「今日回顾」：主动把今天被动记录的对话/会议汇成回顾（陪伴能力）──
function DailyRecapButton({
  tts,
}: {
  tts: ReturnType<typeof useTtsPlayer>;
}): JSX.Element {
  const [loading, setLoading] = useState(false);
  const appendAssistantReply = useStore((s) => s.appendAssistantReply);
  const patchAssistantReply = useStore((s) => s.patchAssistantReply);

  const onRecap = async (): Promise<void> => {
    if (loading) return;
    setLoading(true);
    const replyId = appendAssistantReply(
      "正在回顾今天…",
      "assistant_reply",
      undefined,
      "pending",
    );
    try {
      const r = await getDailyRecap();
      if (r.empty) {
        patchAssistantReply(replyId, {
          text: "今天还没有记录到可回顾的对话或会议。",
          status: "done",
        });
        return;
      }
      const todoCount = r.todos?.length ?? 0;
      const banner =
        todoCount > 0 ? `> 📌 今天有 **${todoCount}** 件待办待跟进\n\n` : "";
      patchAssistantReply(replyId, {
        text: banner + r.recap_markdown,
        status: "done",
      });
      if (tts.enabled) void tts.speak(r.recap_markdown, { interrupt: true });
    } catch (e) {
      const raw = e instanceof Error ? e.message : String(e);
      patchAssistantReply(replyId, { text: `今日回顾失败：${raw}`, status: "failed" });
      message.error("今日回顾失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Tooltip title="今日回顾：把今天的对话与会议汇成一份小结">
      <button
        type="button"
        onClick={() => void onRecap()}
        disabled={loading}
        data-testid="daily-recap"
        aria-label="今日回顾"
        className="flex items-center gap-1 rounded px-1.5 py-0.5 text-ink-500 hover:text-accent hover:bg-paper-200 transition disabled:opacity-50"
      >
        <Sparkles className="w-3.5 h-3.5" />
        <span>{loading ? "回顾中…" : "今日回顾"}</span>
      </button>
    </Tooltip>
  );
}

// ── 顶栏「停止」按钮：中止思考/对话 + 停止 TTS 播放 ─────────────────
// 仅在有运行中的 agent/产物任务、或 TTS 正在播放时出现。
function StopButton({
  tts,
}: {
  tts: ReturnType<typeof useTtsPlayer>;
}): JSX.Element | null {
  const runningCount = useStore((s) => s.runningCount);
  const stopAllRuns = useStore((s) => s.stopAllRuns);
  const active = runningCount > 0 || tts.isSpeaking;
  if (!active) return null;
  const handleStop = (): void => {
    stopAllRuns();
    tts.cancel();
  };
  return (
    <Tooltip title="停止：中止当前思考/对话并停止朗读">
      <button
        type="button"
        onClick={handleStop}
        data-testid="stop-button"
        aria-label="停止"
        className="flex items-center gap-1 rounded px-1.5 py-0.5 text-err hover:bg-err/10 ring-1 ring-err/30 transition"
      >
        <Square className="w-3 h-3 fill-current" />
        <span>停止</span>
      </button>
    </Tooltip>
  );
}

// ── 顶栏 TTS 状态按钮 ──────────────────────────────────────────────
//
// 顶栏 TTS 按钮是唯一用户开关：默认关闭=静默模式；打开后 Echo 回答才自动播放。
// 文案固定为 "TTS"，避免再出现"静音/播放中/停止"多套语义；开关状态只用颜色、
// data-tts-state 和 tooltip 表达。
function TtsTopBarButton({
  tts,
}: {
  tts: ReturnType<typeof useTtsPlayer>;
}): JSX.Element {
  const unhealthy =
    tts.enabled &&
    (tts.lastError !== null ||
      (tts.synthHealth !== null && tts.synthHealth.ok === false));
  const healthDetail =
    tts.lastError ??
    (tts.synthHealth && !tts.synthHealth.ok
      ? `合成检查失败：${tts.synthHealth.detail ?? tts.synthHealth.state}`
      : null);
  const tooltip = tts.isSpeaking
    ? "TTS 正在播放：点击关闭并停止当前播放"
    : !tts.enabled
      ? "TTS 已关（静默模式）：点击打开，Echo 回答会自动播放"
      : unhealthy
        ? (healthDetail ?? "TTS 上游异常")
        : tts.synthHealth?.ok
          ? `TTS 已开：Echo 回答会自动播放（合成 ${tts.synthHealth.latency_ms ?? "?"}ms），点击关闭`
          : "TTS 已开：Echo 回答会自动播放，点击关闭";
  const label = "TTS";
  const color = tts.isSpeaking
    ? "text-accent bg-accent/10 hover:bg-paper-200 ring-1 ring-accent/30"
    : !tts.enabled
      ? "text-ink-400 hover:bg-paper-200"
      : unhealthy
        ? "text-amber-600 hover:bg-paper-200"
        : "text-accent hover:bg-paper-200";
  const Icon = !tts.enabled
      ? VolumeX
      : unhealthy
        ? AlertTriangle
        : Volume2;
  const handleClick = (): void => {
    tts.setEnabled(!tts.enabled);
  };
  return (
    <Tooltip title={tooltip}>
      <button
        type="button"
        onClick={handleClick}
        className={`flex items-center gap-1 rounded px-1.5 py-0.5 transition ${color}`}
        data-testid="tts-toggle"
        data-tts-state={
          !tts.enabled
            ? "disabled"
            : unhealthy
              ? "unhealthy"
              : tts.isSpeaking
                ? "speaking"
                : "ok"
        }
        aria-label={tooltip}
      >
        <Icon className="w-3.5 h-3.5" />
        <span>{label}</span>
      </button>
    </Tooltip>
  );
}

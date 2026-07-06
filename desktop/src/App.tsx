import { Layout, Tooltip } from "antd";
import {
  AlertTriangle,
  MessageSquare,
  Mic,
  Settings,
  Volume2,
  VolumeX,
} from "lucide-react";
import { useState } from "react";
import MeetingList from "@/components/MeetingList";
import TranscriptStream from "@/components/TranscriptStream";
import ArtifactPanel from "@/components/ArtifactPanel";
import MinutesView from "@/components/MinutesView";
import CommandBar from "@/components/CommandBar";
import CaptureStatus from "@/components/CaptureStatus";
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
import { useOnboarding } from "@/hooks/useOnboarding";
import { useMeetingHistory } from "@/hooks/useMeetingHistory";

const { Header, Sider, Content } = Layout;

export default function App(): JSX.Element {
  useEchoWS();
  useMeetingHistory();
  const captureStatus = useEchoCapture();
  const tts = useTtsPlayer();
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const events = useStore((s) => s.events);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialSection, setSettingsInitialSection] = useState<"workspace" | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const onboarding = useOnboarding();

  const openSettings = (section: "workspace" | null = null) => {
    setSettingsInitialSection(section);
    setSettingsOpen(true);
  };

  return (
    <Layout className="echodesk-shell !h-screen !bg-paper-50 !overflow-hidden">
      <Header className="app-header app-drag flex items-center justify-between !bg-paper-50 !px-5 !h-12 border-b border-paper-300 shrink-0">
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
        <div className="app-header-status app-no-drag flex items-center gap-3 text-[11px] text-ink-500">
          <StatusBar
            ttsHealth={tts.synthHealth}
            ttsEnabled={tts.enabled}
            ttsLastError={tts.lastError}
            onRefreshTtsHealth={tts.refreshHealth}
          />
          <span className="app-header-separator w-px h-3 bg-paper-300" aria-hidden />
          <MeetingStatusBar />
          <TtsTopBarButton tts={tts} />
          <span className="app-event-count">事件 {events.length}</span>
          <span className="app-connection-status flex items-center gap-1.5">
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
              onClick={() => openSettings()}
              className="flex min-w-8 min-h-8 items-center justify-center rounded px-1.5 py-1.5 text-ink-500 hover:text-ink-700 hover:bg-paper-200 transition"
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
        initialSection={settingsInitialSection}
        onReplayOnboarding={onboarding.resetForDebug}
      />

      <OnboardingModal
        open={onboarding.shouldShow}
        onClose={onboarding.markCompleted}
      />

      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />

      <WorkspaceBar onOpenSettings={() => openSettings("workspace")} />

      <Layout className="echodesk-main-layout !bg-paper-50 !flex-1 !min-h-0 !overflow-hidden">
        <Sider
          width={260}
          className="echodesk-meeting-sider !bg-paper-150 border-r border-paper-300 !px-2 !py-3 !overflow-hidden flex flex-col min-h-0"
        >
          <div className="echodesk-meeting-title shrink-0 flex items-center gap-1.5 px-2 mb-2 text-ink-500 text-[11px] uppercase tracking-wider">
            <MessageSquare className="w-3 h-3" />
            <span>会议</span>
          </div>
          <MeetingList />
        </Sider>

        <Content className="echodesk-content flex !bg-paper-50 !min-h-0 !overflow-hidden">
          <div className="echodesk-transcript-pane flex-1 min-w-0 min-h-0 border-r border-paper-300 flex flex-col">
            <div className="echodesk-transcript-header flex items-center gap-2 px-6 h-11 border-b border-paper-300 shrink-0">
              <Mic className="w-3.5 h-3.5 text-ink-500" />
              <span
                className="shrink-0 whitespace-nowrap text-[13px] text-ink-700 font-medium"
                data-testid="transcript-title"
              >
                转写流
              </span>
              {currentMeetingId && (
                <span className="ml-2 text-[11px] text-ink-400 font-mono">
                  {currentMeetingId}
                </span>
              )}
              <div className="ml-auto min-w-0 flex justify-end">
                <CaptureStatus status={captureStatus} />
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
              <TranscriptStream />
            </div>
            <div className="shrink-0">
              <CommandBar />
            </div>
          </div>

          <div className="echodesk-output-pane w-[440px] shrink-0 min-h-0 flex flex-col bg-paper-50 overflow-hidden">
            <MinutesView />
            <ArtifactPanel />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

// ── 顶栏 TTS 状态按钮 ──────────────────────────────────────────────
//
// 旧实现只显示 enabled/disabled，绿灯=开、灰=关；用户报"TTS 完全失效"时
// 看到的就是绿灯——欺骗。M_tts_check 改成三态：
//   - enabled + 健康 ok            → 绿 + 「TTS」
//   - enabled + diag 报错 / lastError → 橙 + 三角警告 + 「TTS 异常」
//   - disabled                       → 灰 + 「静音」
//   - 播放中                         → 蓝标 + 「播放中」
// Tooltip / aria-label 都带上 last error / diag detail，方便排错。
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
  const tooltip = !tts.enabled
    ? "TTS 已关"
    : unhealthy
      ? (healthDetail ?? "TTS 上游异常")
      : tts.synthHealth?.ok
        ? `TTS 正常（合成 ${tts.synthHealth.latency_ms ?? "?"}ms）`
        : "TTS 已开：会议纪要 / 回答会语音播报";
  const label = tts.isSpeaking
    ? "播放中"
    : !tts.enabled
      ? "静音"
      : unhealthy
        ? "TTS 异常"
        : "TTS";
  const color = !tts.enabled
    ? "text-ink-400 hover:bg-paper-200"
    : unhealthy
      ? "text-amber-600 hover:bg-paper-200"
      : "text-accent hover:bg-paper-200";
  const Icon = !tts.enabled ? VolumeX : unhealthy ? AlertTriangle : Volume2;
  return (
    <Tooltip title={tooltip}>
      <button
        type="button"
        onClick={() => tts.setEnabled(!tts.enabled)}
        className={`flex min-h-8 items-center gap-1 rounded px-2 py-1.5 transition ${color}`}
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

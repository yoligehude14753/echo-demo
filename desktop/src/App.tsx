import { Drawer, Layout, Tooltip, message } from "antd";
import {
  AlertTriangle,
  AudioWaveform,
  Bot,
  MessageSquare,
  Mic,
  PanelRight,
  PanelRightClose,
  Settings,
  Volume2,
  VolumeX,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import MeetingList from "@/components/MeetingList";
import TranscriptStream from "@/components/TranscriptStream";
import ArtifactPanel from "@/components/ArtifactPanel";
import MinutesView from "@/components/MinutesView";
import CommandBar from "@/components/CommandBar";
import CaptureStatus from "@/components/CaptureStatus";
import MeetingStatusBar from "@/components/MeetingStatusBar";
import WorkspaceBar from "@/components/WorkspaceBar";
import StatusBar from "@/components/StatusBar";
import IdentityStatus from "@/components/IdentityStatus";
import SyncPanel from "@/components/SyncPanel";
import { useSyncWorker } from "@/syncWorker";
import SettingsPanel from "@/components/SettingsPanel";
import OnboardingModal from "@/components/OnboardingModal";
import AboutModal from "@/components/AboutModal";
import { useEchoCapture } from "@/capture/useEchoCapture";
import AndroidCaptureSelector from "@/capture/AndroidCaptureSelector";
import {
  isFreeCapturePreferenceConfigured,
  requestFreeCaptureSetup,
} from "@/capture/freeCaptureMode";
import { useStore } from "@/store";
import { useEchoWS } from "@/ws";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";
import { useOnboarding } from "@/hooks/useOnboarding";
import { useMeetingHistory } from "@/hooks/useMeetingHistory";
import { meetingDisplayTitle } from "@/lib/meetingDisplay";
import {
  type AppUpdateStatus,
  canInstallAppUpdate,
  installAppUpdate,
  isNewerAppUpdate,
  openUpdateTarget,
} from "@/runtime";

const { Header, Sider, Content } = Layout;

type InspectorView = "minutes" | "artifacts";

export default function App(): JSX.Element {
  useEchoWS();
  useSyncWorker();
  useMeetingHistory();
  const appUpdateStatus = useAppUpdateStatus();
  const onboarding = useOnboarding();
  const captureStatus = useEchoCapture({ enabled: !onboarding.shouldShow });
  const tts = useTtsPlayer();
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const currentMeeting = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId] : undefined,
  );
  const events = useStore((s) => s.events);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialSection, setSettingsInitialSection] = useState<"workspace" | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [inspectorView, setInspectorView] = useState<InspectorView>("minutes");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  const inspectorToggleRef = useRef<HTMLButtonElement>(null);
  const initialFreeCaptureSetupRequested = useRef(false);

  useEffect(() => {
    if (onboarding.shouldShow) return;
    if (initialFreeCaptureSetupRequested.current) return;
    if (isFreeCapturePreferenceConfigured()) return;
    // The request is retained until a selector claims it, so mount order cannot
    // lose the first-run setup signal.
    initialFreeCaptureSetupRequested.current = true;
    requestFreeCaptureSetup("first_run");
  }, [onboarding.shouldShow]);

  useEffect(() => {
    const eventType = events[events.length - 1]?.type;
    if (!eventType) return;

    if (eventType.startsWith("minutes.")) {
      setInspectorView("minutes");
      setInspectorOpen(true);
      return;
    }

    if (
      eventType.startsWith("artifact.") ||
      eventType.startsWith("agent.")
    ) {
      setInspectorView("artifacts");
      setInspectorOpen(true);
    }
  }, [events]);

  useEffect(() => {
    if (currentMeetingId) setInspectorView("minutes");
  }, [currentMeetingId]);

  const openSettings = (section: "workspace" | null = null) => {
    setSettingsInitialSection(section);
    setSettingsOpen(true);
  };

  const closeInspector = () => {
    setInspectorOpen(false);
    window.setTimeout(
      () => inspectorToggleRef.current?.focus({ preventScroll: true }),
      200,
    );
  };

  return (
    <Layout className="echodesk-shell !h-screen !bg-paper-50 !overflow-hidden">
      <Header className="app-header app-drag flex items-center justify-between !bg-paper-50 !px-5 !h-12 border-b border-paper-300 shrink-0">
        <div className="flex items-center gap-2.5">
          <span className="app-brand-mark" aria-hidden="true">
            <AudioWaveform className="app-brand-glyph" />
          </span>
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
        <div className="app-update-slot app-no-drag flex flex-1 min-w-0 items-center justify-end px-3">
          <AppUpdateButton status={appUpdateStatus} />
        </div>
        <div className="app-header-status app-no-drag flex items-center gap-2 text-[11px] text-ink-500">
          <IdentityStatus />
          <SyncPanel />
          <div className="app-diagnostics flex min-w-0 items-center gap-2">
            <StatusBar
              ttsHealth={tts.synthHealth}
              ttsEnabled={tts.enabled}
              ttsLastError={tts.lastError}
              onRefreshTtsHealth={tts.refreshHealth}
            />
            <span className="app-connection-status flex items-center gap-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  connected ? "bg-accent" : "bg-err"
                }`}
              />
              {connected ? "已连接" : "断线"}
            </span>
          </div>
          <span className="app-header-separator w-px h-4 bg-paper-300" aria-hidden />
          <MeetingStatusBar />
          <TtsTopBarButton tts={tts} />
          <span className="app-event-count sr-only">事件 {events.length}</span>
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
      <AndroidCaptureSelector />

      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />

      <Drawer
        title="会话"
        placement="left"
        width={Math.min(336, typeof window === "undefined" ? 336 : window.innerWidth - 32)}
        open={mobileSessionsOpen}
        onClose={() => setMobileSessionsOpen(false)}
        rootClassName="mobile-session-drawer"
        data-testid="mobile-session-drawer"
      >
        {mobileSessionsOpen && (
          <MeetingList
            captureState={captureStatus.state}
            onSelect={() => setMobileSessionsOpen(false)}
          />
        )}
      </Drawer>

      <WorkspaceBar onOpenSettings={() => openSettings("workspace")} />

      <Layout className="echodesk-main-layout !bg-paper-50 !flex-1 !min-h-0 !overflow-hidden">
        <Sider
          width={260}
          className="echodesk-meeting-sider !bg-paper-150 border-r border-paper-300 !px-2 !py-3 !overflow-hidden flex flex-col min-h-0"
        >
          <div className="echodesk-meeting-title shrink-0 flex items-center gap-1.5 px-2 mb-2 text-ink-500 text-[11px] uppercase tracking-wider">
            <MessageSquare className="w-3 h-3" />
            <span>会话</span>
          </div>
          <MeetingList captureState={captureStatus.state} />
        </Sider>

        <Content className="echodesk-content flex !bg-paper-50 !min-h-0 !overflow-hidden">
          <div className="echodesk-transcript-pane flex-1 min-w-0 min-h-0 border-r border-paper-300 flex flex-col">
            <div className="echodesk-transcript-header flex items-center gap-2 px-6 h-11 border-b border-paper-300 shrink-0">
              <Tooltip title="打开会话列表">
                <button
                  type="button"
                  className="mobile-session-toggle"
                  onClick={() => setMobileSessionsOpen(true)}
                  aria-label="打开会话列表"
                  data-testid="mobile-session-toggle"
                >
                  <MessageSquare className="h-4 w-4" />
                </button>
              </Tooltip>
              <div
                className="conversation-mode flex min-w-0 items-center gap-3"
                data-testid="conversation-mode"
              >
                <span
                  className="conversation-mode-title inline-flex items-center gap-1.5"
                  data-testid="conversation-mode-title"
                >
                  <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>对话</span>
                </span>
                <span className="conversation-source-legend" data-testid="conversation-source-transcript">
                  <Mic className="h-3 w-3" aria-hidden="true" />
                  转录
                </span>
                <span className="conversation-source-legend conversation-source-legend--ai" data-testid="conversation-source-ai">
                  <Bot className="h-3 w-3" aria-hidden="true" />
                  AI
                </span>
              </div>
              <span className="sr-only" data-testid="transcript-title">
                对话流
              </span>
              {currentMeeting && (
                <span
                  className="current-meeting-title ml-2 max-w-[180px] truncate text-[11px] text-ink-400"
                  title={meetingDisplayTitle(currentMeeting)}
                >
                  {meetingDisplayTitle(currentMeeting)}
                </span>
              )}
              <div className="ml-auto min-w-0 flex justify-end">
                <CaptureStatus status={captureStatus} />
              </div>
              <Tooltip title={inspectorOpen ? "收起检查器" : "打开检查器"}>
                <button
                  ref={inspectorToggleRef}
                  type="button"
                  onClick={() => setInspectorOpen((open) => !open)}
                  className={`inspector-toggle inline-flex min-h-8 min-w-8 items-center justify-center rounded-md ${
                    inspectorOpen ? "is-active" : ""
                  }`}
                  aria-label={inspectorOpen ? "收起检查器" : "打开检查器"}
                  aria-expanded={inspectorOpen}
                  aria-controls="echodesk-inspector"
                  data-testid="inspector-toggle"
                >
                  <PanelRight className="w-4 h-4" />
                </button>
              </Tooltip>
            </div>
            <div
              id="workspace-stream-view"
              className="flex-1 min-h-0 overflow-hidden flex flex-col"
              data-view="conversation"
            >
              <TranscriptStream />
            </div>
            <div className="shrink-0">
              <CommandBar />
            </div>
          </div>

          <div
            id="echodesk-inspector"
            className={`echodesk-output-pane w-[440px] shrink-0 min-h-0 flex flex-col bg-paper-50 overflow-hidden ${
              inspectorOpen ? "is-open" : ""
            }`}
            data-testid="inspector"
          >
            <div className="inspector-header flex shrink-0 items-center">
              <div
                className="inspector-tabs flex min-w-0 flex-1 items-center gap-1"
                role="tablist"
                aria-label="检查器"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={inspectorView === "minutes"}
                  aria-controls="inspector-minutes"
                  onClick={() => setInspectorView("minutes")}
                  className={`inspector-tab ${
                    inspectorView === "minutes" ? "is-active" : ""
                  }`}
                  data-testid="inspector-tab-minutes"
                >
                  会议纪要
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={inspectorView === "artifacts"}
                  aria-controls="inspector-artifacts"
                  onClick={() => setInspectorView("artifacts")}
                  className={`inspector-tab ${
                    inspectorView === "artifacts" ? "is-active" : ""
                  }`}
                  data-testid="inspector-tab-artifacts"
                >
                  工作产物
                </button>
              </div>
              <Tooltip title="收起检查器" placement="left">
                <button
                  type="button"
                  onClick={closeInspector}
                  className="inspector-close"
                  aria-label="收起检查器"
                  aria-controls="echodesk-inspector"
                  data-testid="inspector-close"
                >
                  <PanelRightClose className="h-4 w-4" />
                </button>
              </Tooltip>
            </div>
            <div className="inspector-content flex-1 min-h-0 overflow-hidden">
              <div
                id="inspector-minutes"
                className={`inspector-panel minutes-panel h-full min-h-0 ${
                  inspectorView === "minutes" ? "is-active" : ""
                }`}
                role="tabpanel"
                hidden={inspectorView !== "minutes"}
              >
                <MinutesView />
              </div>
              <div
                id="inspector-artifacts"
                className={`inspector-panel artifacts-panel h-full min-h-0 ${
                  inspectorView === "artifacts" ? "is-active" : ""
                }`}
                role="tabpanel"
                hidden={inspectorView !== "artifacts"}
              >
                <ArtifactPanel />
              </div>
            </div>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

function useAppUpdateStatus(): AppUpdateStatus | null {
  const [status, setStatus] = useState<AppUpdateStatus | null>(null);
  useEffect(() => {
    let cancelled = false;
    const handleStatus = (status: AppUpdateStatus) => {
      if (!cancelled) {
        setStatus(status);
      }
    };

    if (window.echo?.getUpdateStatus) {
      void window.echo.getUpdateStatus().then(handleStatus).catch(() => undefined);
    }
    const unsubscribe = window.echo?.onUpdateStatus?.(handleStatus);
    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, []);
  return status;
}

function shouldShowUpdateButton(status: AppUpdateStatus | null): status is AppUpdateStatus {
  if (!status || !isNewerAppUpdate(status)) return false;
  return (
    canInstallAppUpdate(status) ||
    status.status === "downloading" ||
    status.status === "installing"
  );
}

function updateButtonLabel(status: AppUpdateStatus): string {
  if (status.status === "installing") return "正在安装";
  if (status.status === "downloaded") return "安装并重启";
  if (status.status === "downloading") return `下载中 ${status.percent ?? 0}%`;
  if (status.canAutoInstall === false) return "下载更新";
  return "更新";
}

function AppUpdateButton({ status }: { status: AppUpdateStatus | null }): JSX.Element | null {
  if (!shouldShowUpdateButton(status)) return null;
  const disabled = status.status === "downloading" || status.status === "installing";
  const tooltip =
    status.status === "downloaded"
      ? "更新包已下载，点击后安装并重启"
      : status.canAutoInstall === false
        ? "当前平台需要打开下载页手动更新"
        : status.latestVersion
          ? `发现 EchoDesk v${status.latestVersion}`
          : "发现 EchoDesk 新版本";

  const onClick = async () => {
    if (disabled || !canInstallAppUpdate(status)) return;
    try {
      await installAppUpdate(status);
      if (!status.canAutoInstall) {
        message.info("已打开下载页面");
      }
    } catch (e) {
      console.error("[app-update] install failed", e);
      message.error("更新失败，已保留当前版本");
      if (isNewerAppUpdate(status)) {
        try {
          await openUpdateTarget(status);
        } catch {
          /* ignore */
        }
      }
    }
  };

  return (
    <Tooltip title={tooltip}>
      <button
        type="button"
        onClick={() => void onClick()}
        disabled={disabled}
        className="app-update-button"
        data-testid="app-update-button"
        aria-label={tooltip}
      >
        <span className="app-update-indicator" aria-hidden="true" />
        <span className="truncate">{updateButtonLabel(status)}</span>
      </button>
    </Tooltip>
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
  const notConfigured =
    tts.synthHealth?.state === "not_configured" ||
    tts.synthHealth?.state === "disabled";
  const unhealthy =
    tts.enabled &&
    !notConfigured &&
    (tts.lastError !== null ||
      tts.synthHealth?.ok === false);
  const tooltip = !tts.enabled
    ? "语音播报已关闭"
    : notConfigured
      ? "语音播报未配置（可选），不影响会议、转写或 AI"
    : unhealthy
      ? "语音播报暂时不可用，可在 AI 状态中重新测试"
      : tts.synthHealth?.ok
        ? `语音播报正常（${tts.synthHealth.latency_ms ?? "?"} 毫秒）`
        : "语音播报已开启";
  const label = tts.isSpeaking
    ? "播报中"
    : !tts.enabled
      ? "已静音"
      : notConfigured
        ? "播报未配置"
      : unhealthy
        ? "播报异常"
        : "语音播报";
  const color = !tts.enabled || notConfigured
    ? "text-ink-400 hover:bg-paper-200"
    : unhealthy
      ? "text-amber-600 hover:bg-paper-200"
      : "text-accent hover:bg-paper-200";
  const Icon =
    !tts.enabled || notConfigured
      ? VolumeX
      : unhealthy
        ? AlertTriangle
        : Volume2;
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
            : notConfigured
              ? "not_configured"
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

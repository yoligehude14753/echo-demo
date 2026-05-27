import { Layout, Tooltip } from "antd";
import { MessageSquare, Mic, Settings, Volume2, VolumeX } from "lucide-react";
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
import { useEchoCapture } from "@/capture/useEchoCapture";
import { useStore } from "@/store";
import { useEchoWS } from "@/ws";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";

const { Header, Sider, Content } = Layout;

export default function App(): JSX.Element {
  useEchoWS();
  const captureStatus = useEchoCapture();
  const tts = useTtsPlayer();
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const events = useStore((s) => s.events);
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <Layout className="!h-screen !bg-paper-50 !overflow-hidden">
      <Header className="app-drag flex items-center justify-between !bg-paper-50 !px-5 !h-12 border-b border-paper-300 shrink-0">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full bg-accent shadow-[0_0_0_3px_rgba(16,163,127,0.18)]" />
          <span className="brand font-semibold text-[15px] text-ink-900">
            EchoDesk
          </span>
          <span className="text-[11px] text-ink-500">v0.1</span>
        </div>
        <div className="app-no-drag flex items-center gap-3 text-[11px] text-ink-500">
          <StatusBar />
          <span className="w-px h-3 bg-paper-300" aria-hidden />
          <MeetingStatusBar />
          <Tooltip
            title={tts.enabled ? "TTS 已开：会议纪要/回答会语音播报" : "TTS 已关"}
          >
            <button
              type="button"
              onClick={() => tts.setEnabled(!tts.enabled)}
              className={`flex items-center gap-1 rounded px-1.5 py-0.5 transition ${
                tts.enabled
                  ? "text-accent hover:bg-paper-200"
                  : "text-ink-400 hover:bg-paper-200"
              }`}
              data-testid="tts-toggle"
              aria-label="TTS 开关"
            >
              {tts.enabled ? (
                <Volume2 className="w-3.5 h-3.5" />
              ) : (
                <VolumeX className="w-3.5 h-3.5" />
              )}
              <span>{tts.isSpeaking ? "播放中" : tts.enabled ? "TTS" : "静音"}</span>
            </button>
          </Tooltip>
          <span>事件 {events.length}</span>
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
      />

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
                <span className="ml-2 text-[11px] text-ink-400 font-mono">
                  {currentMeetingId}
                </span>
              )}
              <div className="ml-auto">
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

          <div className="w-[440px] shrink-0 min-h-0 flex flex-col bg-paper-50 overflow-hidden">
            <MinutesView />
            <ArtifactPanel />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

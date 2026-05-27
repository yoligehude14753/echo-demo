import { Layout } from "antd";
import { MessageSquare, Mic } from "lucide-react";
import MeetingList from "@/components/MeetingList";
import TranscriptStream from "@/components/TranscriptStream";
import ArtifactPanel from "@/components/ArtifactPanel";
import MinutesView from "@/components/MinutesView";
import CommandBar from "@/components/CommandBar";
import CaptureStatus from "@/components/CaptureStatus";
import WorkspaceBar from "@/components/WorkspaceBar";
import { useEchoCapture } from "@/capture/useEchoCapture";
import { useStore } from "@/store";
import { useEchoWS } from "@/ws";

const { Header, Sider, Content } = Layout;

export default function App(): JSX.Element {
  useEchoWS();
  const captureStatus = useEchoCapture();
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const events = useStore((s) => s.events);

  return (
    <Layout className="!min-h-screen !bg-paper-50">
      <Header className="app-drag flex items-center justify-between !bg-paper-50 !px-5 !h-12 border-b border-paper-300">
        <div className="flex items-center gap-2.5">
          <span className="w-2 h-2 rounded-full bg-accent shadow-[0_0_0_3px_rgba(16,163,127,0.18)]" />
          <span className="brand font-semibold text-[15px] text-ink-900">
            Echo
          </span>
          <span className="text-[11px] text-ink-500">demo · v0.1</span>
        </div>
        <div className="app-no-drag flex items-center gap-4 text-[11px] text-ink-500">
          <span>事件 {events.length}</span>
          <span className="flex items-center gap-1.5">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                connected ? "bg-accent" : "bg-err"
              }`}
            />
            {connected ? "已连接" : "断线"}
          </span>
        </div>
      </Header>

      <WorkspaceBar />

      <Layout className="!bg-paper-50">
        <Sider
          width={260}
          className="!bg-paper-150 border-r border-paper-300 !px-2 !py-3"
        >
          <div className="flex items-center gap-1.5 px-2 mb-2 text-ink-500 text-[11px] uppercase tracking-wider">
            <MessageSquare className="w-3 h-3" />
            <span>会议</span>
          </div>
          <MeetingList />
        </Sider>

        <Content className="flex !bg-paper-50">
          <div className="flex-1 min-w-0 border-r border-paper-300 flex flex-col">
            <div className="flex items-center gap-2 px-6 h-11 border-b border-paper-300">
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
            <div className="flex-1 min-h-0 overflow-hidden">
              <TranscriptStream />
            </div>
            <CommandBar />
          </div>

          <div className="w-[440px] shrink-0 flex flex-col bg-paper-50">
            <MinutesView />
            <ArtifactPanel />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

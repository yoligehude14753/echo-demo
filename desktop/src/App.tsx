import { useEffect } from "react";
import { Layout, Badge } from "antd";
import { Activity, MessageSquare, FileText, Sparkles } from "lucide-react";
import MeetingList from "@/components/MeetingList";
import TranscriptStream from "@/components/TranscriptStream";
import ArtifactPanel from "@/components/ArtifactPanel";
import MinutesView from "@/components/MinutesView";
import { useStore } from "@/store";
import { useEchoWS } from "@/ws";

const { Header, Sider, Content } = Layout;

export default function App(): JSX.Element {
  useEchoWS();
  const connected = useStore((s) => s.connected);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const events = useStore((s) => s.events);

  useEffect(() => {
    document.documentElement.classList.add("dark");
  }, []);

  return (
    <Layout className="!min-h-screen !bg-bg-900">
      <Header className="flex items-center justify-between !bg-bg-800 border-b border-bg-700 px-6">
        <div className="flex items-center gap-3 text-slate-100">
          <Sparkles className="w-5 h-5 text-accent" />
          <span className="font-semibold tracking-wide">Echo · 数字分身</span>
          <span className="text-xs text-slate-400">demo v0.1</span>
        </div>
        <div className="flex items-center gap-6 text-xs text-slate-400">
          <span className="flex items-center gap-1">
            <Activity className="w-3.5 h-3.5" />
            事件 {events.length}
          </span>
          <Badge
            status={connected ? "success" : "error"}
            text={connected ? "已连接" : "断线重连中..."}
          />
        </div>
      </Header>
      <Layout className="!bg-bg-900">
        <Sider width={280} className="!bg-bg-800 border-r border-bg-700 px-3 py-4">
          <div className="flex items-center gap-2 mb-3 text-slate-300 text-sm">
            <MessageSquare className="w-4 h-4" />
            <span>会议清单</span>
          </div>
          <MeetingList />
        </Sider>
        <Content className="flex">
          <div className="flex-1 min-w-0 border-r border-bg-700">
            <div className="flex items-center gap-2 px-6 py-3 border-b border-bg-700 text-slate-300 text-sm">
              <Activity className="w-4 h-4" />
              <span>转写流</span>
              {currentMeetingId && (
                <span className="ml-auto text-xs text-slate-500">
                  meeting_id: {currentMeetingId}
                </span>
              )}
            </div>
            <TranscriptStream />
          </div>
          <div className="w-[420px] shrink-0 flex flex-col">
            <div className="flex items-center gap-2 px-6 py-3 border-b border-bg-700 text-slate-300 text-sm">
              <FileText className="w-4 h-4" />
              <span>会议纪要 + 产物</span>
            </div>
            <MinutesView />
            <ArtifactPanel />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

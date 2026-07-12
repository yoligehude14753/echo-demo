import { useEffect, useMemo, useState } from "react";
import { Button, Modal, message } from "antd";
import { Copy, Download, ExternalLink, Loader2, QrCode, Trash2 } from "lucide-react";
import { clearMeetingOutputs, meetingShareUrl } from "@/api";
import { meetingDisplayTitle } from "@/lib/meetingDisplay";
import type { GeneratedArtifact, MeetingCard, MeetingMinutes } from "@/types";

interface Props {
  open: boolean;
  meeting: MeetingCard | undefined;
  onClose: () => void;
  onOutputsCleared: (artifactIds: string[]) => void;
}

function uniqueArtifactIds(meeting: MeetingCard | undefined): string[] {
  const ids: string[] = [];
  const add = (id: string | null | undefined): void => {
    if (id && !ids.includes(id)) ids.push(id);
  };
  meeting?.artifacts.forEach((a) => add(a.artifact_id));
  meeting?.minutes?.todos?.forEach((todo) => add(todo.artifact_id));
  return ids;
}

function minutesMarkdown(minutes: MeetingMinutes | undefined, artifacts: GeneratedArtifact[]): string {
  if (!minutes) return "";
  const lines: string[] = [
    `# ${minutes.title}`,
    "",
    `- 时长：${Math.round(minutes.duration_sec)} 秒`,
    `- 生成时间：${new Date(minutes.created_at).toLocaleString()}`,
    "",
    "## 摘要",
    "",
    minutes.summary,
    "",
  ];
  for (const sec of minutes.sections) {
    lines.push(`## ${sec.heading}`, "");
    sec.bullets.forEach((b) => lines.push(`- ${b}`));
    lines.push("");
  }
  if (minutes.decisions.length > 0) {
    lines.push("## 决议", "");
    minutes.decisions.forEach((d) => lines.push(`- ${d}`));
    lines.push("");
  }
  const todos = minutes.todos?.length ? minutes.todos : [];
  if (todos.length > 0) {
    lines.push("## 待办", "");
    todos.forEach((todo) => {
      const state = todo.status === "done" ? "已完成" : "待处理";
      lines.push(`- [${state}] ${todo.text}${todo.assignee ? `（${todo.assignee}）` : ""}`);
    });
    lines.push("");
  }
  if (artifacts.length > 0) {
    lines.push("## 会议产物", "");
    artifacts.forEach((a) => lines.push(`- ${a.title?.trim() || "未命名文件"} (${a.artifact_type})`));
  }
  return `${lines.join("\n").trim()}\n`;
}

function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function safeDownloadName(raw: string): string {
  return raw
    .replace(/[<>:"/\\|?*]+/g, " ")
    .split("")
    .filter((ch) => ch.charCodeAt(0) >= 32)
    .join("")
    .trim();
}

function isLoopbackUrl(raw: string): boolean {
  try {
    const host = new URL(raw).hostname;
    return host === "127.0.0.1" || host === "localhost" || host === "::1";
  } catch {
    return false;
  }
}

export default function MeetingShareModal({
  open,
  meeting,
  onClose,
  onOutputsCleared,
}: Props): JSX.Element {
  const [shareUrl, setShareUrl] = useState("");
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [loadingQr, setLoadingQr] = useState(false);
  const [clearing, setClearing] = useState(false);
  const artifactIds = useMemo(() => uniqueArtifactIds(meeting), [meeting]);
  const artifactCount = meeting?.artifacts.length ?? 0;
  const title = meeting?.minutes?.title || meetingDisplayTitle(meeting, "会议资料");
  const loopbackShareUrl = shareUrl ? isLoopbackUrl(shareUrl) : false;

  useEffect(() => {
    let cancelled = false;
    if (!open || !meeting) {
      setShareUrl("");
      setQrDataUrl("");
      return;
    }
    setLoadingQr(true);
    meetingShareUrl(meeting.meeting_id, artifactIds)
      .then(async (url) => {
        if (cancelled) return;
        setShareUrl(url);
        const QRCode = await import("qrcode");
        const dataUrl = await QRCode.toDataURL(url, {
          width: 320,
          margin: 1,
          errorCorrectionLevel: "M",
          color: {
            dark: "#26211d",
            light: "#ffffff",
          },
        });
        if (!cancelled) setQrDataUrl(dataUrl);
      })
      .catch((e) => {
        console.error("[meeting-share] QR generation failed", e);
        message.error("二维码生成失败，请稍后重新打开");
      })
      .finally(() => {
        if (!cancelled) setLoadingQr(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifactIds, meeting, open]);

  const onCopy = async (): Promise<void> => {
    if (!shareUrl) return;
    await navigator.clipboard.writeText(shareUrl);
    message.success("已复制分享链接");
  };

  const onDownloadMinutes = (): void => {
    if (!meeting?.minutes) {
      message.info("会议纪要尚未生成");
      return;
    }
    const safeTitle = safeDownloadName(title) || "echodesk-minutes";
    downloadText(`${safeTitle}.md`, minutesMarkdown(meeting.minutes, meeting.artifacts));
  };

  const onClear = (): void => {
    if (!meeting) return;
    Modal.confirm({
      title: "删除本会议输出？",
      content: `将清空本会议纪要，并删除 ${artifactIds.length} 个已知产物文件。该操作不可撤回。`,
      okText: "删除",
      okType: "danger",
      cancelText: "取消",
      onOk: async () => {
        setClearing(true);
        try {
          await clearMeetingOutputs(meeting.meeting_id, artifactIds);
          onOutputsCleared(artifactIds);
          message.success("本会议输出已删除");
          onClose();
        } finally {
          setClearing(false);
        }
      },
    });
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={520}
      title={
        <span className="inline-flex items-center gap-2">
          <QrCode className="w-4 h-4 text-accent" />
          <span>扫码保存会议资料</span>
        </span>
      }
    >
      <div className="space-y-4" data-testid="meeting-share-modal">
        <div className="min-w-0">
          <div className="text-[15px] font-semibold text-ink-900 truncate">{title}</div>
        </div>

        <div className="flex flex-col items-center justify-center rounded-lg border border-paper-300 bg-white py-5 min-h-[260px]">
          {loadingQr ? (
            <div className="flex flex-col items-center gap-2 text-ink-500">
              <Loader2 className="w-8 h-8 animate-spin" />
              <span className="text-[12px]">正在生成二维码…</span>
            </div>
          ) : qrDataUrl ? (
            <img
              src={qrDataUrl}
              alt="会议资料二维码"
              className="w-56 h-56"
              data-testid="meeting-share-qr"
            />
          ) : (
            <span className="text-[12px] text-ink-400">暂无二维码</span>
          )}
        </div>

        <div
          className="rounded-md bg-paper-100 border border-paper-300 px-3 py-2 text-[11px] text-ink-500 break-all"
          data-testid="meeting-share-url"
        >
          {shareUrl || "等待生成分享链接"}
        </div>

        <div
          className={`rounded-md px-3 py-2 text-[12px] leading-relaxed ${
            loopbackShareUrl
              ? "bg-red-50 text-red-700 border border-red-100"
              : "bg-emerald-50 text-emerald-700 border border-emerald-100"
          }`}
          data-testid="meeting-share-network-hint"
        >
          {loopbackShareUrl
            ? "当前开发预览链接只能在本机打开；请使用已安装的 EchoDesk 再让其他设备扫码。"
            : "手机或电视和这台电脑在同一网络时，可扫码打开并保存纪要、下载产物。"}
        </div>

        <div className="grid grid-cols-2 gap-2 text-[12px] text-ink-500">
          <div className="rounded-md bg-paper-100 px-3 py-2">
            纪要：{meeting?.minutes ? "已生成" : "未生成"}
          </div>
          <div className="rounded-md bg-paper-100 px-3 py-2">
            产物：{artifactCount} 个
          </div>
        </div>

        <div className="flex flex-wrap justify-between gap-2 pt-1">
          <span className="flex flex-wrap gap-2">
            <Button
              size="small"
              icon={<Copy className="w-3.5 h-3.5" />}
              onClick={onCopy}
              disabled={!shareUrl}
            >
              复制链接
            </Button>
            <Button
              size="small"
              icon={<ExternalLink className="w-3.5 h-3.5" />}
              onClick={() => shareUrl && window.open(shareUrl, "_blank", "noopener,noreferrer")}
              disabled={!shareUrl}
            >
              打开
            </Button>
            <Button
              size="small"
              icon={<Download className="w-3.5 h-3.5" />}
              onClick={onDownloadMinutes}
              data-testid="download-minutes-btn"
            >
              下载纪要
            </Button>
          </span>
          <Button
            danger
            size="small"
            icon={<Trash2 className="w-3.5 h-3.5" />}
            loading={clearing}
            onClick={onClear}
            disabled={!meeting}
            data-testid="clear-meeting-outputs-btn"
          >
            删除输出
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * CaptureStatus — CaptureSession 状态（纯展示，无控制按钮）
 *
 * 文案设计要点（Phase 4 修复"已转 4266 但 0 段入库"）：
 *  - "采集"   = 已上传的 chunk 数（含 VAD/底噪/STT 空文本，未入库）
 *  - "入库"   = 真正写入 ambient_segments 表的有效段数
 *
 * M_diag_brake：
 *  - hover 弹 Popover 展示 7 道门处理结果分布表（实时根因分布）
 *  - STT 短暂不可用时仅内部退避，不在主界面打断展示
 */
import { useEffect, useState } from "react";
import { Popover, Progress, Tag } from "antd";
import { Loader2 } from "lucide-react";

import type {
  CaptureStatsSnapshot,
  CaptureStatus as CaptureStatusModel,
} from "@/domain/session";

interface Props {
  status: CaptureStatusModel;
}

const INIT_DISPLAY_TIMEOUT_MS = 20_000;
const INIT_TIMEOUT_TEXT =
  "初始化超时；问答、知识库、联网搜索和文档生成仍可继续使用";

interface DoorRow {
  key: keyof CaptureStatsSnapshot;
  label: string;
  /** circuit_open / failed 等坏路径着色为红色，引导用户看根因。 */
  tone: "neutral" | "warn" | "danger" | "good";
}

const DOORS: DoorRow[] = [
  { key: "gated_rms", label: "静音或音量过低", tone: "neutral" },
  { key: "gated_low_speech", label: "有效语音不足", tone: "neutral" },
  { key: "stt_circuit_open", label: "识别服务暂时不可用", tone: "danger" },
  { key: "stt_failed", label: "语音识别失败", tone: "danger" },
  { key: "stt_empty", label: "未识别出文字", tone: "warn" },
  { key: "hallu_dropped", label: "已过滤异常文本", tone: "warn" },
  { key: "stored", label: "已保存转写", tone: "good" },
];

function toneColor(tone: DoorRow["tone"]): string {
  switch (tone) {
    case "danger":
      return "#dc2626";
    case "warn":
      return "#d97706";
    case "good":
      return "#16a34a";
    default:
      return "#6b7280";
  }
}

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "刚刚";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  return `${h} 小时前`;
}

function shouldShowMicRetry(errorMessage: string | null | undefined): boolean {
  if (!errorMessage) return true;
  return !/(USB|蓝牙|有效输入|电视麦克风|系统识别|原生录音不可用)/i.test(errorMessage);
}

function displayMicError(errorMessage: string | null | undefined): string {
  if (!errorMessage) return "";
  if (/requested device not found|device not found|notfounderror/i.test(errorMessage)) {
    return "未找到可用麦克风，请检查系统输入设备";
  }
  if (/permission denied|notallowederror|denied/i.test(errorMessage)) {
    return "系统未授权麦克风，请在 macOS 隐私设置中允许 EchoDesk";
  }
  if (/timeout|超时/i.test(errorMessage)) {
    return "麦克风初始化超时";
  }
  if (/not supported|notsupportederror/i.test(errorMessage)) {
    return "当前环境不支持音频采集，请使用 EchoDesk 桌面应用";
  }
  if (/(USB|蓝牙|有效输入|silent PCM|microphone input)/i.test(errorMessage)) {
    return "请接入 USB/蓝牙会议麦克风";
  }
  return "无法访问音频输入，请检查麦克风权限和输入设备";
}

function DoorBreakdown({
  stats,
  chunksDroppedCircuit,
}: {
  stats: CaptureStatsSnapshot | null;
  chunksDroppedCircuit: number;
}): JSX.Element {
  if (!stats) {
    return (
      <div className="text-xs text-slate-500 py-2">加载诊断数据中…</div>
    );
  }
  const total = Math.max(1, stats.chunks_total); // 防 /0
  return (
    <div className="w-[340px] space-y-2 text-xs">
      <div className="font-medium text-slate-700">本次转写处理结果</div>
      <div className="grid grid-cols-2 gap-1.5 text-[11px]">
        <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1">
          <div className="text-slate-500">环境音量</div>
          <div className="text-slate-800">
            {stats.last_rms < 120 ? "偏低" : "正常"}
          </div>
        </div>
        <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1">
          <div className="text-slate-500">有效语音占比</div>
          <div className="tabular-nums text-slate-800">
            {Math.round(stats.last_speech_ratio * 100)}%
          </div>
        </div>
      </div>
      <table className="w-full">
        <tbody>
          {DOORS.map((d) => {
            const n = stats[d.key] as number;
            const pct = (n / total) * 100;
            return (
              <tr key={d.key}>
                <td className="py-0.5 pr-2 text-slate-600 whitespace-nowrap">
                  {d.label}
                </td>
                <td className="py-0.5 pr-2 w-[120px]">
                  <Progress
                    percent={pct}
                    showInfo={false}
                    strokeColor={toneColor(d.tone)}
                    size="small"
                  />
                </td>
                <td
                  className="py-0.5 text-right tabular-nums"
                  style={{ color: n > 0 ? toneColor(d.tone) : "#94a3b8" }}
                >
                  {n}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="flex justify-between text-slate-500 pt-1.5 border-t border-slate-200">
        <span>已处理片段</span>
        <span className="tabular-nums">{stats.chunks_total}</span>
      </div>
      {chunksDroppedCircuit > 0 && (
        <div className="flex justify-between text-red-600">
          <span>服务不可用期间未识别</span>
          <span className="tabular-nums">{chunksDroppedCircuit}</span>
        </div>
      )}
      <div className="flex justify-between text-slate-500">
        <span>最近采集</span>
        <span>{formatRelative(stats.last_chunk_at)}</span>
      </div>
      <div className="flex justify-between text-slate-500">
        <span>最近保存</span>
        <span>{formatRelative(stats.last_stored_at)}</span>
      </div>
    </div>
  );
}

export default function CaptureStatus({ status }: Props): JSX.Element {
  const {
    state,
    ambientChunks,
    ambientStored,
    meetingChunks,
    meetingOverlayId,
    errorMessage,
    sttCircuitOpenUntil,
    chunksDroppedCircuit,
    stats,
  } = status;
  const [initializingTooLong, setInitializingTooLong] = useState(false);

  useEffect(() => {
    if (state !== "initializing") {
      setInitializingTooLong(false);
      return;
    }
    const t = window.setTimeout(
      () => setInitializingTooLong(true),
      INIT_DISPLAY_TIMEOUT_MS,
    );
    return () => window.clearTimeout(t);
  }, [state]);

  if (state === "initializing") {
    if (initializingTooLong) {
      return (
        <Tag color="red" data-testid="capture-status" tabIndex={-1}>
          麦克风不可用 · {INIT_TIMEOUT_TEXT}
        </Tag>
      );
    }
    return (
      <Tag
        className="!border-accent/25 !bg-accent/5 !text-accentDark"
        icon={<Loader2 className="w-3 h-3 animate-spin" />}
        data-testid="capture-status"
        tabIndex={-1}
      >
        初始化麦克风…
      </Tag>
    );
  }

  if (state === "error") {
    const retryHint = shouldShowMicRetry(errorMessage) ? "系统会自动重试" : "";
    const displayError = displayMicError(errorMessage);
    return (
      <Popover
        placement="bottomRight"
        trigger={["hover", "click"]}
        content={
          <div className="max-w-[320px] text-[12px] leading-5 text-ink-700">
            <div className="font-medium text-ink-900">{displayError}</div>
            {retryHint && <div className="mt-1 text-ink-500">{retryHint}</div>}
          </div>
        }
      >
        <Tag color="red" data-testid="capture-status" tabIndex={0}>
          麦克风不可用
        </Tag>
      </Popover>
    );
  }

  const circuitOpen =
    sttCircuitOpenUntil !== null && sttCircuitOpenUntil > Date.now();
  const ariaLabel = meetingOverlayId
      ? `正在转写，已采集 ${ambientChunks} 段，已保存 ${ambientStored} 段，会议中已记录 ${meetingChunks} 段`
      : `正在转写，已采集 ${ambientChunks} 段，已保存 ${ambientStored} 段，静音和底噪会自动过滤`;

  return (
    <Popover
      placement="bottomRight"
      content={
        <div className="w-[340px] space-y-3">
          <div
            className={`grid gap-1.5 text-[11px] ${
              meetingOverlayId ? "grid-cols-3" : "grid-cols-2"
            }`}
          >
            <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1.5">
              <div className="text-slate-500">已采集</div>
              <div className="text-slate-800 tabular-nums">
                {ambientChunks} 段
              </div>
            </div>
            <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1.5">
              <div className="text-slate-500">已保存转写</div>
              <div className="text-slate-800 tabular-nums">
                {ambientStored} 段
              </div>
            </div>
            {meetingOverlayId && (
              <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1.5">
                <div className="text-slate-500">会议段落</div>
                <div className="text-slate-800 tabular-nums">
                  {meetingChunks} 段
                </div>
              </div>
            )}
          </div>
          <DoorBreakdown
            stats={stats}
            chunksDroppedCircuit={chunksDroppedCircuit}
          />
        </div>
      }
      mouseEnterDelay={0.2}
      trigger={["hover", "click"]}
    >
      <Tag
        className={`!m-0 cursor-help ${
          circuitOpen
            ? "!border-amber-300/60 !bg-amber-50 !text-amber-700"
            : "!border-accent/25 !bg-accent/5 !text-accentDark"
        }`}
        data-testid="capture-status"
        data-circuit-open={circuitOpen ? "1" : "0"}
        aria-label={ariaLabel}
        tabIndex={0}
      >
        <span className="inline-flex items-center gap-1.5">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              circuitOpen ? "bg-amber-500" : "bg-accent"
            }`}
            aria-hidden="true"
          />
          <span>{circuitOpen ? "语音识别暂时不可用" : "正在转写"}</span>
          <span className="sr-only">
            正在转写 · 已采集 {ambientChunks} · 已保存 {ambientStored}
            {meetingOverlayId
              ? ` · 会议中 · 段 ${meetingChunks}`
              : " · 静音/底噪自动过滤"}
          </span>
        </span>
      </Tag>
    </Popover>
  );
}

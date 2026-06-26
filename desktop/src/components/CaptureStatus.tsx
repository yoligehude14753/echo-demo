/**
 * CaptureStatus — CaptureSession 状态（纯展示，无控制按钮）
 *
 * 文案设计要点（Phase 4 修复"已转 4266 但 0 段入库"）：
 *  - "采集"   = 已上传的 chunk 数（含 VAD/底噪/STT 空文本，未入库）
 *  - "入库"   = 真正写入 ambient_segments 表的有效段数
 *
 * M_diag_brake：
 *  - hover 弹 Popover 展示 7 道门处理结果分布表（实时根因分布）
 *  - STT 熔断时 Tag 变红 + 倒计时 + 红色 banner
 */
import { useEffect, useState } from "react";
import { Popover, Progress, Tag } from "antd";
import { AlertTriangle, Loader2 } from "lucide-react";

import type {
  CaptureStatsSnapshot,
  CaptureStatus as CaptureStatusModel,
} from "@/domain/session";

interface Props {
  status: CaptureStatusModel;
}

interface DoorRow {
  key: keyof CaptureStatsSnapshot;
  label: string;
  /** circuit_open / failed 等坏路径着色为红色，引导用户看根因。 */
  tone: "neutral" | "warn" | "danger" | "good";
}

const DOORS: DoorRow[] = [
  { key: "gated_rms", label: "整段 RMS 过低", tone: "neutral" },
  { key: "gated_low_speech", label: "活跃帧率不足", tone: "neutral" },
  { key: "stt_circuit_open", label: "历史熔断拒绝", tone: "danger" },
  { key: "stt_failed", label: "STT 调用失败", tone: "danger" },
  { key: "stt_empty", label: "STT 返回空", tone: "warn" },
  { key: "hallu_dropped", label: "幻觉门丢弃", tone: "warn" },
  { key: "stored", label: "成功入库", tone: "good" },
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

function formatCountdown(retryAtMs: number): string {
  const s = Math.max(0, Math.round((retryAtMs - Date.now()) / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${(s % 60).toString().padStart(2, "0")}s`;
}

function shouldShowMicRetry(errorMessage: string | null | undefined): boolean {
  if (!errorMessage) return true;
  return !/(USB|蓝牙|有效输入|电视麦克风|系统识别|原生录音不可用)/i.test(errorMessage);
}

function displayMicError(errorMessage: string | null | undefined): string {
  if (!errorMessage) return "";
  if (/(USB|蓝牙|有效输入|silent PCM|microphone input)/i.test(errorMessage)) {
    return "请接入 USB/蓝牙会议麦克风";
  }
  return errorMessage;
}

function DoorBreakdown({
  stats,
  chunksDroppedCircuit,
  circuitOpenUntil,
}: {
  stats: CaptureStatsSnapshot | null;
  chunksDroppedCircuit: number;
  circuitOpenUntil: number | null;
}): JSX.Element {
  if (!stats) {
    return (
      <div className="text-xs text-slate-500 py-2">加载诊断数据中…</div>
    );
  }
  const total = Math.max(1, stats.chunks_total); // 防 /0
  return (
    <div className="w-[340px] space-y-2 text-xs">
      {circuitOpenUntil !== null && (
        <div className="flex items-center gap-1.5 px-2 py-1.5 rounded bg-red-50 text-red-700 border border-red-200">
          <AlertTriangle className="w-3.5 h-3.5 flex-none" />
          <span className="font-medium">
            云端 STT 熔断 · 暂停上传 · {formatCountdown(circuitOpenUntil)} 后探测
          </span>
        </div>
      )}
      <div className="font-medium text-slate-700">
        7 道门处理结果（进程级 in-memory）
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1">
          <div className="text-slate-500">最近 RMS</div>
          <div className="font-mono text-slate-800">{Math.round(stats.last_rms)}</div>
        </div>
        <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1">
          <div className="text-slate-500">活跃帧率</div>
          <div className="font-mono text-slate-800">
            {Math.round(stats.last_speech_ratio * 100)}%
          </div>
        </div>
        <div className="rounded bg-slate-50 border border-slate-200 px-2 py-1">
          <div className="text-slate-500">最近门控</div>
          <div className="font-mono text-slate-800 truncate">
            {stats.last_gate_reason ?? "—"}
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
        <span>合计 chunk</span>
        <span className="tabular-nums">{stats.chunks_total}</span>
      </div>
      {chunksDroppedCircuit > 0 && (
        <div className="flex justify-between text-red-600">
          <span>熔断期间已丢弃</span>
          <span className="tabular-nums">{chunksDroppedCircuit}</span>
        </div>
      )}
      <div className="flex justify-between text-slate-500">
        <span>最近 chunk</span>
        <span>{formatRelative(stats.last_chunk_at)}</span>
      </div>
      <div className="flex justify-between text-slate-500">
        <span>最近入库</span>
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
  // 让倒计时每秒刷新
  const [, setTick] = useState(0);
  useEffect(() => {
    if (sttCircuitOpenUntil === null) return;
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [sttCircuitOpenUntil]);

  if (state === "initializing") {
    return (
      <Tag
        color="blue"
        icon={<Loader2 className="w-3 h-3 animate-spin" />}
        data-testid="capture-status"
        tabIndex={-1}
      >
        初始化麦克风…
      </Tag>
    );
  }

  if (state === "error") {
    const retryHint = shouldShowMicRetry(errorMessage) ? " · 5s 后重试" : "";
    const displayError = displayMicError(errorMessage);
    return (
      <Tag color="red" data-testid="capture-status" tabIndex={-1}>
        麦克风不可用
        {displayError ? ` · ${displayError}` : ""}
        {retryHint}
      </Tag>
    );
  }

  const circuitOpen =
    sttCircuitOpenUntil !== null && sttCircuitOpenUntil > Date.now();
  const ariaLabel = circuitOpen
    ? `云端 STT 熔断，已采集 ${ambientChunks} 段，入库 ${ambientStored} 段，丢弃 ${chunksDroppedCircuit} 段`
    : meetingOverlayId
      ? `持续采集中，已采集 ${ambientChunks} 段，入库 ${ambientStored} 段，会议中已上传 ${meetingChunks} 段`
      : `持续采集中，已采集 ${ambientChunks} 段，入库 ${ambientStored} 段（静音/底噪自动过滤）`;

  return (
    <Popover
      placement="bottomRight"
      content={
        <DoorBreakdown
          stats={stats}
          chunksDroppedCircuit={chunksDroppedCircuit}
          circuitOpenUntil={circuitOpen ? sttCircuitOpenUntil : null}
        />
      }
      mouseEnterDelay={0.2}
      trigger="hover"
    >
      <Tag
        color={circuitOpen ? "error" : "red"}
        className="!m-0 cursor-help"
        data-testid="capture-status"
        data-circuit-open={circuitOpen ? "1" : "0"}
        aria-label={ariaLabel}
        tabIndex={-1}
      >
        <span className="inline-flex items-center gap-1.5">
          {circuitOpen ? (
            <>
              <AlertTriangle className="w-3 h-3" />
              <span className="font-medium">STT 熔断</span>
              <span className="text-[10px] opacity-80 tabular-nums">
                · {formatCountdown(sttCircuitOpenUntil)} 后探测
              </span>
              <span className="text-[10px] opacity-70">
                · 丢弃 {chunksDroppedCircuit}
              </span>
            </>
          ) : (
            <>
              <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
              持续采集
              <span className="text-[10px] opacity-80">
                · 采集 {ambientChunks} · 入库 {ambientStored}
              </span>
              {meetingOverlayId ? (
                <span className="text-[10px] opacity-80">
                  · 会议中 · 段 {meetingChunks}
                </span>
              ) : (
                <span className="text-[10px] opacity-70">
                  · 静音/底噪自动过滤
                </span>
              )}
            </>
          )}
        </span>
      </Tag>
    </Popover>
  );
}

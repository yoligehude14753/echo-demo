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

import type { CaptureStatsSnapshot } from "@/domain/session";
import type {
  CaptureGateReason,
  CaptureViewModel,
} from "@/capture/captureOperationalState";
import { requestFreeCaptureSetup } from "@/capture/freeCaptureMode";

interface Props {
  status: CaptureViewModel;
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
  { key: "repeat_dropped", label: "已过滤重复文本", tone: "warn" },
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

function gateReasonLabel(reason: CaptureGateReason | null): string {
  switch (reason) {
    case "ok":
      return "有效语音已通过";
    case "rms_too_low":
      return "输入音量偏低";
    case "speech_ratio_too_low":
      return "有效语音不足";
    case "unknown":
      return "暂无法判定";
    default:
      return "旧服务未提供";
  }
}

function formatEpochRelative(epoch: number | null): string {
  return epoch === null ? "—" : formatRelative(new Date(epoch).toISOString());
}

function primaryOperationalLabel(status: CaptureViewModel): string {
  if (status.transport.warning === "upload_unavailable") {
    return "上传暂时不可用";
  }
  if (status.transport.warning === "backpressure") {
    return "待发送片段较多";
  }
  if (status.admission.warning === "rms_too_low") {
    return "输入音量偏低";
  }
  if (status.admission.warning === "speech_ratio_too_low") {
    return "有效语音不足";
  }
  if (status.freshness.warning === "stats_unavailable") {
    return "诊断数据更新中断";
  }
  return "";
}

function OperationalBreakdown({ status }: { status: CaptureViewModel }): JSX.Element {
  const { transport, freshness, admission } = status;
  const ratio = admission.acceptedSpeechRatio;
  const frameCount =
    admission.acceptedSpeechFrames !== null &&
    admission.observedAudioFrames !== null
      ? `${admission.acceptedSpeechFrames}/${admission.observedAudioFrames}`
      : "旧服务未提供";
  const operationalLabel = primaryOperationalLabel(status);

  return (
    <div
      className="space-y-2 border-t border-slate-200 pt-2 text-xs"
      data-testid="capture-operational-breakdown"
    >
      <div className="font-medium text-slate-700">采集运行状态</div>
      <div className="space-y-1 text-slate-600">
        <div className="flex justify-between gap-3">
          <span>上传轴</span>
          <span data-testid="capture-transport-summary">
            {transport.warning === "none" ? "正常" : operationalLabel}
            · 待发送 {transport.queueDepth}/{transport.queueCapacity}
          </span>
        </div>
        <div className="flex justify-between gap-3">
          <span>上传确认</span>
          <span className="tabular-nums">
            {transport.acknowledged}/{transport.sent} · 最近成功 {formatEpochRelative(transport.lastSuccessfulUploadAt)}
          </span>
        </div>
        {transport.droppedBackpressure > 0 && (
          <div className="flex justify-between gap-3 text-amber-700">
            <span>队列已满丢弃</span>
            <span className="tabular-nums">{transport.droppedBackpressure}</span>
          </div>
        )}
        <div className="flex justify-between gap-3">
          <span>诊断刷新轴</span>
          <span data-testid="capture-freshness-summary">
            {freshness.warning === "none"
              ? freshness.source === "legacy"
                ? "旧服务兼容"
                : "正常"
              : "更新中断"}
            {freshness.lastSequence !== null
              ? ` · 游标 ${freshness.lastSequence}`
              : ""}
          </span>
        </div>
        <div className="flex justify-between gap-3">
          <span>输入轴</span>
          <span data-testid="capture-admission-summary">
            {admission.warning === "none"
              ? gateReasonLabel(admission.lastGateReason)
              : gateReasonLabel(admission.warning)}
            {ratio === null ? " · 比例旧服务未提供" : ` · 有效语音 ${Math.round(ratio * 100)}%`}
          </span>
        </div>
        <div className="flex justify-between gap-3">
          <span>有效帧（分子/分母）</span>
          <span className="tabular-nums">{frameCount}</span>
        </div>
      </div>
      {transport.warning === "upload_unavailable" && (
        <div className="text-amber-700">正在等待真实片段确认恢复。</div>
      )}
      {transport.warning === "backpressure" && (
        <div className="text-amber-700">发送队列已满，正在排空过期片段。</div>
      )}
      {admission.warning === "rms_too_low" && (
        <div className="text-amber-700">请提高输入音量或检查当前麦克风。</div>
      )}
      {admission.warning === "speech_ratio_too_low" && (
        <div className="text-amber-700">请靠近麦克风并减少背景噪声。</div>
      )}
      {freshness.warning === "stats_unavailable" && (
        <div className="text-amber-700">诊断数据暂时没有更新，上传状态仍单独计算。</div>
      )}
    </div>
  );
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
    runtimeState,
    ambientChunks,
    ambientStored,
    meetingChunks,
    meetingOverlayId,
    errorMessage,
    sttCircuitOpenUntil,
    chunksDroppedCircuit,
    stats,
    transport,
    freshness,
    admission,
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

  if (runtimeState === "off") {
    return (
      <Tag
        className="!border-paper-300 !bg-paper-100 !text-ink-500"
        data-testid="capture-status"
        tabIndex={-1}
      >
        自由收音已暂停
      </Tag>
    );
  }

  if (runtimeState === "device_not_selected") {
    return (
      <Tag
        className="!border-paper-300 !bg-paper-100 !text-ink-500"
        data-testid="capture-status"
        tabIndex={-1}
      >
        本设备未选为收音端
        <button
          type="button"
          className="ml-1 underline"
          onClick={() => requestFreeCaptureSetup("first_run")}
        >
          选择收音设备
        </button>
      </Tag>
    );
  }

  if (runtimeState === "permission_required") {
    return (
      <Tag color="red" data-testid="capture-status" tabIndex={-1}>
        麦克风未授权
        <button
          type="button"
          className="ml-1 underline"
          onClick={() => void window.echo?.openMicSystemPrefs?.()}
        >
          打开系统麦克风设置
        </button>
      </Tag>
    );
  }

  if (state === "initializing" || runtimeState === "free_starting") {
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
  const operationalLabel = primaryOperationalLabel(status);
  const modeLabel =
    runtimeState === "formal_recording"
      ? "正式会议中"
      : runtimeState === "speech_detected"
        ? "检测到语音"
        : runtimeState === "offline_buffering"
          ? "离线缓存中"
          : "自由收音中";
  const statusLabel =
    operationalLabel || (circuitOpen ? "语音识别暂时不可用" : modeLabel);
  const ariaLabel = meetingOverlayId
      ? `${statusLabel}，已采集 ${ambientChunks} 段，已保存 ${ambientStored} 段，会议中已记录 ${meetingChunks} 段`
      : `${statusLabel}，已采集 ${ambientChunks} 段，已保存 ${ambientStored} 段，静音和底噪会自动过滤`;
  const hasOperationalWarning =
    transport.warning !== "none" ||
    freshness.warning !== "none" ||
    admission.warning !== "none";

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
          <OperationalBreakdown status={status} />
        </div>
      }
      mouseEnterDelay={0.2}
      trigger={["hover", "click"]}
    >
      <Tag
        className={`!m-0 cursor-help ${
          circuitOpen || hasOperationalWarning
            ? "!border-amber-300/60 !bg-amber-50 !text-amber-700"
            : "!border-accent/25 !bg-accent/5 !text-accentDark"
        }`}
        data-testid="capture-status"
        data-circuit-open={circuitOpen ? "1" : "0"}
        data-transport-warning={transport.warning}
        data-freshness-warning={freshness.warning}
        data-audio-warning={admission.warning}
        data-queue-depth={transport.queueDepth}
        aria-label={ariaLabel}
        tabIndex={0}
      >
        <span className="inline-flex items-center gap-1.5">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              circuitOpen || hasOperationalWarning ? "bg-amber-500" : "bg-accent"
            }`}
            aria-hidden="true"
          />
          <span>{statusLabel}</span>
          <span className="sr-only">
            {statusLabel} · 已采集 {ambientChunks} · 已保存 {ambientStored}
            {meetingOverlayId
              ? ` · 会议中 · 段 ${meetingChunks}`
              : " · 静音/底噪自动过滤"}
          </span>
        </span>
      </Tag>
    </Popover>
  );
}

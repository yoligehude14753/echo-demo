/**
 * StatusBar · Phase 2 P2.1
 *
 * 顶部 4 个 status pill：backend / heyi-bj / Yunwu / mic
 * 每个 pill：
 *   - 颜色：绿(ok) / 橙(warn 含部分降级 / 缺 key / 重启中) / 红(fail) / 灰(unknown)
 *   - 点开 popover：详细诊断信息（version / latency / 错误）
 *   - backend pill degraded 时多一个"重启 backend"按钮
 *
 * 数据源：useBackendHealth hook（合并 supervisor IPC + /healthz/full + mic perm）
 */

import { Tooltip, Popover, Button } from "antd";
import { RefreshCw, Mic, Server, Cloud, Cpu } from "lucide-react";
import { useMemo } from "react";
import {
  useBackendHealth,
  type BackendHealth,
  type HealthzFull,
  type ProbeResultDTO,
  type SupervisorStatus,
  type MicPermission,
} from "@/hooks/useBackendHealth";

type Level = "ok" | "warn" | "fail" | "unknown";

const COLORS: Record<Level, { dot: string; text: string; ring: string }> = {
  ok: {
    dot: "bg-accent",
    text: "text-ink-700",
    ring: "shadow-[0_0_0_3px_rgba(16,163,127,0.18)]",
  },
  warn: {
    dot: "bg-amber-500",
    text: "text-ink-700",
    ring: "shadow-[0_0_0_3px_rgba(245,158,11,0.18)]",
  },
  fail: {
    dot: "bg-err",
    text: "text-ink-700",
    ring: "shadow-[0_0_0_3px_rgba(220,38,38,0.18)]",
  },
  unknown: {
    dot: "bg-ink-300",
    text: "text-ink-500",
    ring: "",
  },
};

// 用 backend 远程探针 key（跟 backend/app/api/health.py:_probe_all 对齐）
const HEYI_PROBES = ["heyi_stt_firered", "heyi_tts_qwen3", "heyi_llm_fast"] as const;

// ===== 等级聚合 =====

function levelFromSupervisor(s: SupervisorStatus, healthzOk: boolean): Level {
  if (s.state === "ready" || s.state === "external") return healthzOk ? "ok" : "warn";
  if (s.state === "starting" || s.state === "restarting") return "warn";
  if (
    s.state === "degraded" ||
    s.state === "python-not-found" ||
    s.state === "backend-source-not-found"
  )
    return "fail";
  if (s.state === "shutting-down") return "warn";
  return "unknown";
}

function levelFromProbes(probes: ProbeResultDTO[]): Level {
  if (probes.length === 0) return "unknown";
  const okCount = probes.filter((p) => p.ok === true).length;
  const failCount = probes.filter((p) => p.ok === false).length;
  if (failCount === 0 && okCount === probes.length) return "ok";
  if (failCount === probes.length) return "fail";
  return "warn";
}

function levelFromYunwu(p: ProbeResultDTO | undefined): Level {
  if (!p) return "unknown";
  if (p.ok === true) return "ok";
  if (p.ok === false) return "fail";
  // ok=null 通常是 no_api_key：功能不可用但不是"挂了"，标 warn 引导用户填 key
  return "warn";
}

function levelFromMic(p: MicPermission): Level {
  if (p === "granted") return "ok";
  if (p === "denied") return "fail";
  if (p === "prompt") return "warn";
  return "unknown";
}

// ===== Pill UI =====

interface PillProps {
  label: string;
  level: Level;
  icon: JSX.Element;
  tooltip?: string;
  popover: JSX.Element;
  testId: string;
}

function Pill({ label, level, icon, tooltip, popover, testId }: PillProps): JSX.Element {
  const c = COLORS[level];
  const button = (
    <button
      type="button"
      data-testid={testId}
      className={`app-no-drag flex items-center gap-1 rounded px-1.5 py-0.5 transition hover:bg-paper-200 ${c.text}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${c.dot} ${level === "ok" ? c.ring : ""}`} />
      <span className="text-[11px] flex items-center gap-0.5">
        {icon}
        {label}
      </span>
    </button>
  );
  const trigger = (
    <Popover content={popover} placement="bottom" trigger="click">
      {tooltip ? <Tooltip title={tooltip}>{button}</Tooltip> : button}
    </Popover>
  );
  return trigger;
}

// ===== Popover 内容 =====

function fmtLatency(p?: ProbeResultDTO): string {
  if (!p || p.latency_ms === undefined) return "—";
  return `${p.latency_ms}ms`;
}

function fmtCheckedAgo(p?: ProbeResultDTO): string {
  if (!p || !p.checked_at) return "—";
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - p.checked_at));
  if (sec < 60) return `${sec}s 前`;
  return `${Math.floor(sec / 60)} 分钟前`;
}

function ProbeRow({
  name,
  probe,
}: {
  name: string;
  probe: ProbeResultDTO | undefined;
}): JSX.Element {
  if (!probe) {
    return (
      <div className="flex items-center justify-between text-[11px] text-ink-500">
        <span>{name}</span>
        <span>—</span>
      </div>
    );
  }
  const color =
    probe.ok === true ? "text-accent" : probe.ok === false ? "text-err" : "text-amber-500";
  const status =
    probe.ok === true
      ? `ok · ${fmtLatency(probe)}`
      : probe.ok === false
        ? `fail · ${probe.error ?? "unknown"}`
        : (probe.reason ?? "n/a");
  return (
    <div className="flex items-center justify-between text-[11px]">
      <span className="text-ink-700">{name}</span>
      <span className={color}>{status}</span>
    </div>
  );
}

function BackendPopover({
  health,
}: {
  health: BackendHealth;
}): JSX.Element {
  const { supervisor, healthz, healthzOk, manualRestart } = health;
  const isFailState =
    supervisor.state === "degraded" ||
    supervisor.state === "python-not-found" ||
    supervisor.state === "backend-source-not-found";

  return (
    <div className="min-w-[260px] text-[12px] py-1">
      <div className="font-semibold mb-1.5 flex items-center gap-1.5">
        <Server className="w-3.5 h-3.5" />
        Backend
      </div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-ink-500">supervisor</span>
        <span className="font-mono">{supervisor.state}</span>
      </div>
      {healthz?.backend && (
        <>
          <div className="flex items-center justify-between mb-1">
            <span className="text-ink-500">version</span>
            <span className="font-mono">{healthz.backend.version}</span>
          </div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-ink-500">port</span>
            <span className="font-mono">{healthz.backend.port}</span>
          </div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-ink-500">uptime</span>
            <span className="font-mono">{Math.floor(healthz.backend.uptime_s)}s</span>
          </div>
        </>
      )}
      {healthz?.db && (
        <div className="flex items-center justify-between mb-1">
          <span className="text-ink-500">db</span>
          <span className={healthz.db.ok ? "text-accent" : "text-err"}>
            {healthz.db.ok ? `${healthz.db.size_mb ?? "?"}MB` : (healthz.db.error ?? "fail")}
          </span>
        </div>
      )}
      {!healthzOk && (
        <div className="text-err text-[11px] mt-1">
          ⚠ /healthz/full 暂时不通（最近 2 次失败）
        </div>
      )}
      {supervisor.reason && (
        <div className="text-ink-500 text-[11px] mt-1">原因：{supervisor.reason}</div>
      )}
      {supervisor.searched && supervisor.searched.length > 0 && (
        <div className="text-ink-500 text-[10px] mt-1 font-mono">
          已搜索：
          <div className="ml-2 mt-0.5 text-ink-400 truncate">
            {supervisor.searched.join("\n")}
          </div>
        </div>
      )}
      {isFailState && (
        <Button
          size="small"
          type="primary"
          icon={<RefreshCw className="w-3 h-3" />}
          className="!mt-2 !text-[11px]"
          onClick={() => void manualRestart()}
          data-testid="backend-manual-restart"
        >
          重启 backend
        </Button>
      )}
    </div>
  );
}

function HeyiPopover({
  remote,
}: {
  remote: HealthzFull["remote"] | undefined;
}): JSX.Element {
  const stt = remote?.heyi_stt_firered;
  const tts = remote?.heyi_tts_qwen3;
  const fastLlm = remote?.heyi_llm_fast;
  return (
    <div className="min-w-[260px] text-[12px] py-1">
      <div className="font-semibold mb-1.5 flex items-center gap-1.5">
        <Cpu className="w-3.5 h-3.5" />
        heyi-bj 远端服务
      </div>
      <ProbeRow name="STT FireRed :8090" probe={stt} />
      <ProbeRow name="TTS Qwen3 :8094" probe={tts} />
      <ProbeRow name="Fast LLM :7860" probe={fastLlm} />
      <div className="text-ink-400 text-[10px] mt-2">
        探针 30s 一轮 · 最近 {fmtCheckedAgo(stt)}
      </div>
    </div>
  );
}

function YunwuPopover({
  remote,
}: {
  remote: HealthzFull["remote"] | undefined;
}): JSX.Element {
  const yunwu = remote?.yunwu_llm_main;
  const tavily = remote?.tavily;
  return (
    <div className="min-w-[260px] text-[12px] py-1">
      <div className="font-semibold mb-1.5 flex items-center gap-1.5">
        <Cloud className="w-3.5 h-3.5" />
        云端依赖
      </div>
      <ProbeRow name="Yunwu MiniMax-M2.7" probe={yunwu} />
      <ProbeRow name="Tavily 搜索" probe={tavily} />
      {(yunwu?.reason === "no_api_key" || tavily?.reason === "no_api_key") && (
        <div className="text-amber-600 text-[10px] mt-2">
          ⚠ 部分密钥未配置，相关功能（@生成/纪要/@查）将不可用。
          <br />
          编辑 ~/.echodesk/config.json 填入即可（重启 app 生效）。
        </div>
      )}
    </div>
  );
}

function MicPopover({ perm }: { perm: MicPermission }): JSX.Element {
  return (
    <div className="min-w-[260px] text-[12px] py-1">
      <div className="font-semibold mb-1.5 flex items-center gap-1.5">
        <Mic className="w-3.5 h-3.5" />
        麦克风
      </div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-ink-500">权限状态</span>
        <span
          className={
            perm === "granted"
              ? "text-accent"
              : perm === "denied"
                ? "text-err"
                : "text-amber-500"
          }
        >
          {perm}
        </span>
      </div>
      {perm === "denied" && (
        <div className="text-err text-[11px] mt-1.5">
          已被拒绝。请到 系统设置 → 隐私与安全 → 麦克风 勾选 EchoDesk
        </div>
      )}
      {perm === "prompt" && (
        <div className="text-ink-500 text-[11px] mt-1.5">
          尚未授权；首次录音时系统会弹窗，点击"允许"即可。
        </div>
      )}
      {perm === "unknown" && (
        <div className="text-ink-500 text-[11px] mt-1.5">
          浏览器/Electron 未暴露权限 API，请直接尝试录音。
        </div>
      )}
    </div>
  );
}

// ===== 顶层组件 =====

export default function StatusBar(): JSX.Element {
  const health = useBackendHealth();
  const { supervisor, healthz, healthzOk, mic } = health;

  const backendLevel = levelFromSupervisor(supervisor, healthzOk);
  const heyiLevel = useMemo(() => {
    if (!healthz?.remote) return "unknown" as Level;
    return levelFromProbes(HEYI_PROBES.map((k) => healthz.remote[k]).filter(Boolean));
  }, [healthz?.remote]);
  const yunwuLevel = levelFromYunwu(healthz?.remote?.yunwu_llm_main);
  const micLevel = levelFromMic(mic);

  const supervisorPretty: Record<SupervisorStatus["state"], string> = {
    ready: "ok",
    external: "外部",
    starting: "启动中",
    restarting: `重启 ${supervisor.attempt ?? 1}/3`,
    degraded: "降级",
    "python-not-found": "无 Python",
    "backend-source-not-found": "无源码",
    "shutting-down": "退出中",
    unknown: "未知",
  };

  return (
    <div className="flex items-center gap-2" data-testid="status-bar">
      <Pill
        label={`backend ${supervisorPretty[supervisor.state]}`}
        level={backendLevel}
        icon={<Server className="w-3 h-3" />}
        popover={<BackendPopover health={health} />}
        testId="pill-backend"
      />
      <Pill
        label="heyi-bj"
        level={heyiLevel}
        icon={<Cpu className="w-3 h-3" />}
        popover={<HeyiPopover remote={healthz?.remote} />}
        testId="pill-heyi"
      />
      <Pill
        label="云"
        level={yunwuLevel}
        icon={<Cloud className="w-3 h-3" />}
        popover={<YunwuPopover remote={healthz?.remote} />}
        testId="pill-yunwu"
      />
      <Pill
        label="麦克风"
        level={micLevel}
        icon={<Mic className="w-3 h-3" />}
        popover={<MicPopover perm={mic} />}
        testId="pill-mic"
      />
    </div>
  );
}

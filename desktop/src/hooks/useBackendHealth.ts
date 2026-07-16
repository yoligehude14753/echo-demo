/**
 * useBackendHealth · Phase 2 P2.1
 *
 * 把 4 个独立数据源合成 status pill 需要的状态：
 *  - BackendSupervisor IPC（Electron main 推送的 backend 进程生命周期）
 *  - local/dev: GET /healthz/full（backend + host 级诊断）
 *  - public: anonymous GET /bootstrap + /healthz（仅服务可达性）
 *  - navigator.permissions.query({name: "microphone"})（mic 权限）
 *  - WebSocket 连接状态从 store 拿（已有的 connected）
 *
 * 5s 轮询已经足够频繁（backend 探针本身 30s 一轮，再快也不会有新数据）。
 * 失败重试用指数 backoff，避免 backend 死的时候疯狂打。
 */

import { useEffect, useRef, useState } from "react";
import { apiUrl, isPublicRuntime } from "@/runtime";
import { apiTransport, bootstrapBackend } from "@/session";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";

// === supervisor 推送 ===

export type SupervisorState =
  | "starting"
  | "ready"
  | "restarting"
  | "degraded"
  | "python-not-found"
  | "backend-source-not-found"
  | "bundled-backend-unavailable"
  | "shutting-down"
  | "external"
  | "unknown";

export interface SupervisorStatus {
  state: SupervisorState;
  port?: number;
  attempt?: number;
  backoff_ms?: number;
  reason?: string;
  reason_code?: string;
  help_url?: string;
  attempts?: number;
}

// === /healthz/full ===

export interface ProbeResultDTO {
  ok: boolean | null;
  required?: boolean;
  latency_ms?: number;
  error?: string;
  reason?: string;
  provider?: string;
  model?: string;
  endpoint?: string;
  checked_at?: number;
}

export interface HealthzFull {
  backend: { ok: boolean; version?: string; port?: number; uptime_s?: number };
  db?: { ok: boolean; size_mb?: number; path?: string; error?: string };
  remote?: Record<string, ProbeResultDTO>;
  mic: { ok: "unknown" | boolean };
}

export type MicPermission = "granted" | "denied" | "prompt" | "unknown";

export interface BackendHealth {
  // 顶层 supervisor 状态（pretty 字符串 + 颜色等级由 component 决定）
  supervisor: SupervisorStatus;
  // 最近一次健康快照（public 只包含 backend.ok + mic unknown）
  healthz: HealthzFull | null;
  // 健康端点是否成功（独立于 healthz 内容，因为请求本身可能 timeout）
  healthzOk: boolean;
  // mic 权限（Electron 默认应该 granted，但用户能在系统设置撤销）
  mic: MicPermission;
  // renderer 主动触发重启 backend
  manualRestart: () => Promise<void>;
  manualRestartBusy: boolean;
}

// window.echo 全局声明在 runtime.ts；这里我们把 supervisor status 从 unknown 窄化到具体类型

const POLL_INTERVAL_MS = 5000;
const POLL_TIMEOUT_MS = 12_000;
const PUBLIC_BACKEND_POLL_TIMEOUT_MS = 15_000;
// healthz 失败连续 N 次后才视为"真断"，避免单次抖动导致 pill 闪红
const HEALTHZ_FAIL_THRESHOLD = 3;

function healthzTimeoutMs(): number {
  return isPublicRuntime() ? PUBLIC_BACKEND_POLL_TIMEOUT_MS : POLL_TIMEOUT_MS;
}

async function fetchHealthz(signal?: AbortSignal): Promise<HealthzFull | null> {
  try {
    const bootstrap = await bootstrapBackend();
    const publicHealth = isPublicRuntime() || bootstrap?.session_required === true;
    const url = await apiUrl(publicHealth ? "/healthz" : "/healthz/full");
    const res = await apiTransport(url, { cache: "no-store", signal }, {
      timeoutMs: healthzTimeoutMs(),
      throwHttpErrors: false,
      anonymous: publicHealth,
    });
    if (!res.ok) return null;
    if (publicHealth) {
      const body = (await res.json()) as { status?: unknown };
      if (body.status !== "ok") return null;
      return {
        backend: { ok: true },
        mic: { ok: "unknown" },
      };
    }
    return (await res.json()) as HealthzFull;
  } catch {
    return null;
  }
}

async function fetchMicPermission(): Promise<MicPermission> {
  if (typeof navigator === "undefined" || !navigator.permissions) return "unknown";
  try {
    // mac Electron 实测：返回 granted/denied/prompt（跟系统 TCC 实际一致）
    const status = await navigator.permissions.query({
      name: "microphone" as PermissionName,
    });
    return status.state as MicPermission;
  } catch {
    return "unknown";
  }
}

export function useBackendHealth(): BackendHealth {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const [supervisor, setSupervisor] = useState<SupervisorStatus>({
    state: "unknown",
  });
  const [healthz, setHealthz] = useState<HealthzFull | null>(null);
  const [healthzOk, setHealthzOk] = useState(false);
  const [mic, setMic] = useState<MicPermission>("unknown");
  const [manualRestartBusy, setManualRestartBusy] = useState(false);
  const failCountRef = useRef(0);

  useEffect(() => {
    if (!window.echo?.onBackendStatus) return;
    return window.echo.onBackendStatus((s) => {
      // runtime.ts 把 callback param 声明成 unknown，这里运行时验证下 state 字段
      if (s && typeof s === "object" && "state" in s) {
        const status = s as SupervisorStatus;
        setSupervisor(status);
        if (
          status.state === "ready" ||
          status.state === "external" ||
          status.state === "degraded" ||
          status.state === "python-not-found" ||
          status.state === "backend-source-not-found" ||
          status.state === "bundled-backend-unavailable"
        ) {
          setManualRestartBusy(false);
        }
      }
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    failCountRef.current = 0;
    setHealthz(null);
    setHealthzOk(false);

    const tick = async () => {
      const data = await fetchHealthz(controller.signal);
      if (
        cancelled ||
        !isCurrent(originGeneration) ||
        controller.signal.aborted
      ) return;
      if (data) {
        failCountRef.current = 0;
        setHealthz(data);
        setHealthzOk(true);
      } else {
        failCountRef.current += 1;
        if (failCountRef.current >= HEALTHZ_FAIL_THRESHOLD) {
          setHealthzOk(false);
        }
      }
      if (!cancelled) timer = setTimeout(tick, POLL_INTERVAL_MS);
    };
    tick();

    return () => {
      cancelled = true;
      unregisterController();
      if (timer) clearTimeout(timer);
    };
  }, [
    backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  ]);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      const p = await fetchMicPermission();
      if (!cancelled) setMic(p);
    };
    refresh();
    // 用户可能在运行时去 System Settings 改权限，每 15s 重查一遍
    const id = setInterval(refresh, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const manualRestart = async () => {
    if (!window.echo?.manualRestartBackend || manualRestartBusy) return;
    setManualRestartBusy(true);
    try {
      await window.echo.manualRestartBackend();
    } catch {
      // supervisor 自己会通过 IPC 推送下一个状态；这里失败不阻塞 UI
    } finally {
      setManualRestartBusy(false);
    }
  };

  const healthzProvesBackendReady = healthzOk && healthz?.backend?.ok;
  const supervisorLooksStale =
    supervisor.state === "unknown" ||
    supervisor.state === "degraded" ||
    supervisor.state === "python-not-found" ||
    supervisor.state === "backend-source-not-found" ||
    supervisor.state === "bundled-backend-unavailable";

  const effectiveSupervisor: SupervisorStatus =
    healthzProvesBackendReady && supervisorLooksStale
      ? {
          state: "external",
          port: healthz.backend.port,
          reason: "connected through backend health; supervisor state was stale",
        }
      : supervisor;

  return {
    supervisor: effectiveSupervisor,
    healthz,
    healthzOk,
    mic,
    manualRestart,
    manualRestartBusy,
  };
}

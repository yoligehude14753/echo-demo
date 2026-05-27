/**
 * useBackendHealth · Phase 2 P2.1
 *
 * 把 4 个独立数据源合成 status pill 需要的状态：
 *  - BackendSupervisor IPC（Electron main 推送的 backend 进程生命周期）
 *  - GET /healthz/full（backend 自己 + 5 个远程依赖的探针缓存）
 *  - navigator.permissions.query({name: "microphone"})（mic 权限）
 *  - WebSocket 连接状态从 store 拿（已有的 connected）
 *
 * 5s 轮询 /healthz/full 已经足够频繁（backend 探针本身 30s 一轮，再快也不会有新数据）。
 * 失败重试用指数 backoff，避免 backend 死的时候疯狂打。
 */

import { useEffect, useRef, useState } from "react";
import { apiUrl } from "@/runtime";

// === supervisor 推送 ===

export type SupervisorState =
  | "starting"
  | "ready"
  | "restarting"
  | "degraded"
  | "python-not-found"
  | "backend-source-not-found"
  | "shutting-down"
  | "external"
  | "unknown";

export interface SupervisorStatus {
  state: SupervisorState;
  port?: number;
  attempt?: number;
  backoff_ms?: number;
  reason?: string;
  searched?: string[];
  last_error?: string;
}

// === /healthz/full ===

export interface ProbeResultDTO {
  ok: boolean | null;
  latency_ms?: number;
  error?: string;
  reason?: string;
  checked_at?: number;
}

export interface HealthzFull {
  backend: { ok: boolean; version: string; port: number; uptime_s: number };
  db: { ok: boolean; size_mb?: number; path?: string; error?: string };
  remote: Record<string, ProbeResultDTO>;
  mic: { ok: "unknown" | boolean };
}

export type MicPermission = "granted" | "denied" | "prompt" | "unknown";

export interface BackendHealth {
  // 顶层 supervisor 状态（pretty 字符串 + 颜色等级由 component 决定）
  supervisor: SupervisorStatus;
  // 最近一次 /healthz/full 响应（null = 尚未拿到 / 全 down）
  healthz: HealthzFull | null;
  // /healthz/full 是否成功（独立于 healthz 内容，因为请求本身可能 timeout）
  healthzOk: boolean;
  // mic 权限（Electron 默认应该 granted，但用户能在系统设置撤销）
  mic: MicPermission;
  // renderer 主动触发重启 backend
  manualRestart: () => Promise<void>;
}

// window.echo 全局声明在 runtime.ts；这里我们把 supervisor status 从 unknown 窄化到具体类型

const POLL_INTERVAL_MS = 5000;
const POLL_TIMEOUT_MS = 3000;
// healthz 失败连续 N 次后才视为"真断"，避免单次抖动导致 pill 闪红
const HEALTHZ_FAIL_THRESHOLD = 2;

async function fetchHealthz(): Promise<HealthzFull | null> {
  try {
    const url = await apiUrl("/healthz/full");
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), POLL_TIMEOUT_MS);
    try {
      const res = await fetch(url, { signal: ctl.signal });
      if (!res.ok) return null;
      return (await res.json()) as HealthzFull;
    } finally {
      clearTimeout(timer);
    }
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
  const [supervisor, setSupervisor] = useState<SupervisorStatus>({
    state: "unknown",
  });
  const [healthz, setHealthz] = useState<HealthzFull | null>(null);
  const [healthzOk, setHealthzOk] = useState(false);
  const [mic, setMic] = useState<MicPermission>("unknown");
  const failCountRef = useRef(0);

  useEffect(() => {
    if (!window.echo?.onBackendStatus) return;
    return window.echo.onBackendStatus((s) => {
      // runtime.ts 把 callback param 声明成 unknown，这里运行时验证下 state 字段
      if (s && typeof s === "object" && "state" in s) {
        setSupervisor(s as SupervisorStatus);
      }
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      const data = await fetchHealthz();
      if (cancelled) return;
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
      if (timer) clearTimeout(timer);
    };
  }, []);

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
    if (!window.echo?.manualRestartBackend) return;
    try {
      await window.echo.manualRestartBackend();
    } catch {
      // supervisor 自己会通过 IPC 推送下一个状态；这里失败不阻塞 UI
    }
  };

  return { supervisor, healthz, healthzOk, mic, manualRestart };
}

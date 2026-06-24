/**
 * WebSocket 客户端（PR-14 / m5-t3）：
 *
 * 协议（见 backend/app/schemas/events.py）：
 *   client → server: {"type":"client_hello","last_seq":int}
 *   server → client: {"type":"server_hello", payload:{max_seq,version}}
 *                    {"type":"server_ping",  payload:{max_seq}}     每 15s
 *                    {"type":"server_resync",payload:{...}}         history 已淘汰
 *                    业务事件 EchoEvent (seq++)
 *
 * 抖动恢复：
 * - 记录每条业务事件的 seq；重连时发 client_hello(last_seq)
 * - server_resync 时清缓存 + 全量重订阅
 * - 超过 WS_INACTIVE_RECONNECT_MS 没收到任何消息 → 主动重连
 */
import { useEffect, useRef } from "react";
import { useStore } from "@/store";
import {
  type EchoEvent,
  WS_INACTIVE_RECONNECT_MS,
} from "@/types";
import { backendWsUrl, shouldHideSharedPublicHistory } from "@/runtime";

type ConnState = "connecting" | "open" | "closed";

export function useEchoWS(): void {
  const setConnected = useStore((s) => s.setConnected);
  const applyEvent = useStore((s) => s.applyEvent);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const stopRef = useRef(false);
  const lastSeqRef = useRef(0);
  const replayFenceSeqRef = useRef(0);
  const appStartedAtRef = useRef(Date.now());
  const lastActivityRef = useRef(Date.now());
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stateRef = useRef<ConnState>("closed");

  useEffect(() => {
    stopRef.current = false;
    const protocolHandled = new Set<string>([
      "server_hello",
      "server_ping",
      "server_resync",
    ]);

    const stopWatchdog = (): void => {
      if (watchdogRef.current) {
        clearInterval(watchdogRef.current);
        watchdogRef.current = null;
      }
    };

    const startWatchdog = (): void => {
      stopWatchdog();
      watchdogRef.current = setInterval(() => {
        if (stateRef.current !== "open") return;
        const idle = Date.now() - lastActivityRef.current;
        if (idle > WS_INACTIVE_RECONNECT_MS) {
          console.warn(`[ws] inactive ${idle}ms → reconnect`);
          try {
            wsRef.current?.close(4000, "watchdog idle");
          } catch {
            /* ignore */
          }
        }
      }, 5_000);
    };

    const noSharedReplay = shouldHideSharedPublicHistory();

    const isSharedPublicBusinessEvent = (e: EchoEvent): boolean => {
      if (!noSharedReplay) return false;
      if (typeof e.seq === "number" && e.seq <= replayFenceSeqRef.current) {
        return true;
      }
      const ts = Date.parse(e.ts);
      if (Number.isFinite(ts) && ts < appStartedAtRef.current - 10_000) {
        return true;
      }
      // 当前公共 backend 还没有 per-device/client_id 事件隔离。Android/TV
      // 公共演示包只能相信本机 capture/chunk 返回的文本，不能消费共享 WS 业务事件，
      // 否则新安装设备会立刻显示其它设备的会议、纪要和产物。
      return !e.type.startsWith("server_") && e.type !== "error";
    };

    const handleProtocol = (e: EchoEvent): void => {
      if (e.type === "server_resync") {
        console.warn("[ws] server_resync, drop client cache", e.payload);
        lastSeqRef.current = 0;
        useStore.getState().reset();
      } else if (e.type === "server_hello") {
        const max = (e.payload as { max_seq?: number })?.max_seq ?? 0;
        if (noSharedReplay) {
          replayFenceSeqRef.current = Math.max(replayFenceSeqRef.current, max);
        }
        if (max < lastSeqRef.current) lastSeqRef.current = max;
      }
      // server_ping: 啥也不做，watchdog 已在 onmessage 刷新 lastActivity
    };

    const connect = async (): Promise<void> => {
      if (stopRef.current) return;
      stateRef.current = "connecting";
      const url = await backendWsUrl();
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        stateRef.current = "open";
        setConnected(true);
        retryRef.current = 0;
        lastActivityRef.current = Date.now();
        try {
          ws.send(
            JSON.stringify({
              type: "client_hello",
              last_seq: lastSeqRef.current,
              client_version: noSharedReplay
                ? "echodesk-native-public-no-replay"
                : "desktop-1.0",
            })
          );
        } catch (err) {
          console.error("[ws] send client_hello failed", err);
        }
        startWatchdog();
      };

      ws.onmessage = (evt) => {
        lastActivityRef.current = Date.now();
        let data: EchoEvent | null = null;
        try {
          data = JSON.parse(evt.data) as EchoEvent;
        } catch {
          return;
        }
        if (!data || typeof data.type !== "string") return;

        if (typeof data.seq === "number" && data.seq > lastSeqRef.current) {
          lastSeqRef.current = data.seq;
        }
        if (protocolHandled.has(data.type)) {
          handleProtocol(data);
          return;
        }
        if (isSharedPublicBusinessEvent(data)) {
          return;
        }
        applyEvent(data);
      };

      ws.onerror = () => {
        /* close handler 会触发重连 */
      };

      ws.onclose = () => {
        stateRef.current = "closed";
        setConnected(false);
        stopWatchdog();
        if (stopRef.current) return;
        retryRef.current = Math.min(retryRef.current + 1, 8);
        const backoff = Math.min(500 * 2 ** retryRef.current, 8_000);
        setTimeout(() => {
          void connect();
        }, backoff);
      };
    };

    void connect();

    return () => {
      stopRef.current = true;
      stopWatchdog();
      wsRef.current?.close();
    };
  }, [setConnected, applyEvent]);
}

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
import {
  SESSION_IDENTITY_EVENT,
  authenticatedWsConnection,
  ensureServerSession,
  isIdentityLostError,
  type SessionIdentityStatus,
} from "@/session";

type ConnState = "connecting" | "open" | "closed";

interface WsCursor {
  epoch: string | null;
  seq: number;
}

const WS_CURSOR_PREFIX = "echodesk.wsCursor.v1";

function cursorStorageKey(url: string, principalScope: string): string {
  const endpoint = new URL(url);
  return `${WS_CURSOR_PREFIX}:${endpoint.origin}${endpoint.pathname}:${principalScope}`;
}

function loadCursor(key: string): WsCursor {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) ?? "null") as Partial<WsCursor>;
    return {
      epoch: typeof parsed?.epoch === "string" ? parsed.epoch : null,
      seq:
        typeof parsed?.seq === "number" && Number.isSafeInteger(parsed.seq) && parsed.seq >= 0
          ? parsed.seq
          : 0,
    };
  } catch {
    return { epoch: null, seq: 0 };
  }
}

function saveCursor(key: string, cursor: WsCursor): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(cursor));
  } catch {
    // Storage can be disabled; in-memory refs still preserve reconnect continuity.
  }
}

export function useEchoWS(): void {
  const setConnected = useStore((s) => s.setConnected);
  const applyEvent = useStore((s) => s.applyEvent);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const stopRef = useRef(false);
  const lastSeqRef = useRef(0);
  const streamEpochRef = useRef<string | null>(null);
  const cursorKeyRef = useRef<string | null>(null);
  const lastRehydrateFenceRef = useRef<string | null>(null);
  const lastActivityRef = useRef(Date.now());
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stateRef = useRef<ConnState>("closed");
  const authBlockedRef = useRef(false);

  useEffect(() => {
    stopRef.current = false;
    const protocolHandled = new Set<string>([
      "server_hello",
      "server_ping",
      "server_resync",
      "server_sync",
    ]);

    const persistCursor = (): void => {
      if (!cursorKeyRef.current) return;
      saveCursor(cursorKeyRef.current, {
        epoch: streamEpochRef.current,
        seq: lastSeqRef.current,
      });
    };

    const requestRehydrate = (epoch: string | null, fenceSeq: number): void => {
      const key = `${epoch ?? "unknown"}:${fenceSeq}`;
      if (lastRehydrateFenceRef.current === key) return;
      lastRehydrateFenceRef.current = key;
      useStore.getState().requestRehydrate(fenceSeq);
    };

    const stopWatchdog = (): void => {
      if (watchdogRef.current) {
        clearInterval(watchdogRef.current);
        watchdogRef.current = null;
      }
    };

    const stopReconnectTimer = (): void => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
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

    const handleProtocol = (e: EchoEvent): void => {
      if (e.type === "server_resync") {
        console.warn("[ws] server_resync, rehydrate from REST", e.payload);
        const payload = e.payload as {
          reason?: string;
          fence_seq?: number;
          max_seq?: number;
          stream_epoch?: string;
        };
        const nextEpoch = e.stream_epoch ?? payload.stream_epoch ?? null;
        if (
          payload.reason === "stream_epoch_changed" ||
          (streamEpochRef.current && nextEpoch && streamEpochRef.current !== nextEpoch)
        ) {
          useStore.getState().reset();
        }
        streamEpochRef.current = nextEpoch;
        lastSeqRef.current = 0;
        requestRehydrate(nextEpoch, payload.fence_seq ?? payload.max_seq ?? 0);
      } else if (e.type === "server_sync") {
        const payload = e.payload as { fence_seq?: number; stream_epoch?: string };
        const fenceSeq = payload.fence_seq ?? e.seq ?? 0;
        streamEpochRef.current = e.stream_epoch ?? payload.stream_epoch ?? null;
        lastSeqRef.current = Math.max(0, fenceSeq);
        persistCursor();
        requestRehydrate(streamEpochRef.current, lastSeqRef.current);
      } else if (e.type === "server_hello") {
        const payload = e.payload as { max_seq?: number; stream_epoch?: string };
        const max = payload.max_seq ?? 0;
        const serverEpoch = e.stream_epoch ?? payload.stream_epoch ?? null;
        if (
          streamEpochRef.current &&
          serverEpoch &&
          streamEpochRef.current !== serverEpoch
        ) {
          useStore.getState().reset();
          lastSeqRef.current = 0;
          requestRehydrate(serverEpoch, max);
        }
        streamEpochRef.current = serverEpoch;
        if (max < lastSeqRef.current) lastSeqRef.current = max;
        persistCursor();
      }
      // server_ping: 啥也不做，watchdog 已在 onmessage 刷新 lastActivity
    };

    const scheduleReconnect = (delayMs?: number): void => {
      if (stopRef.current || authBlockedRef.current) return;
      stopReconnectTimer();
      retryRef.current = Math.min(retryRef.current + 1, 8);
      const backoff =
        delayMs ?? Math.min(500 * 2 ** retryRef.current, 8_000);
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        void connect();
      }, backoff);
    };

    const connect = async (): Promise<void> => {
      if (
        stopRef.current ||
        authBlockedRef.current ||
        stateRef.current === "connecting" ||
        stateRef.current === "open"
      ) {
        return;
      }
      stateRef.current = "connecting";
      let connection: Awaited<ReturnType<typeof authenticatedWsConnection>>;
      try {
        connection = await authenticatedWsConnection();
      } catch (error) {
        stateRef.current = "closed";
        setConnected(false);
        stopWatchdog();
        if (isIdentityLostError(error)) {
          authBlockedRef.current = true;
          stopReconnectTimer();
          return;
        }
        scheduleReconnect();
        return;
      }
      const { url, token, cursorKey } = connection;
      const storageKey = cursorStorageKey(url, cursorKey);
      if (cursorKeyRef.current !== storageKey) {
        const stored = loadCursor(storageKey);
        cursorKeyRef.current = storageKey;
        streamEpochRef.current = stored.epoch;
        lastSeqRef.current = stored.seq;
        lastRehydrateFenceRef.current = null;
      }
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        authBlockedRef.current = false;
        stateRef.current = "open";
        setConnected(true);
        retryRef.current = 0;
        lastActivityRef.current = Date.now();
        try {
          ws.send(
            JSON.stringify({
              type: "client_hello",
              last_seq: lastSeqRef.current,
              stream_epoch: streamEpochRef.current ?? undefined,
              client_version: "echodesk-0.3",
              auth: token ? { type: "bearer", token } : undefined,
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

        if (protocolHandled.has(data.type)) {
          handleProtocol(data);
          return;
        }
        if (
          data.stream_epoch &&
          streamEpochRef.current &&
          data.stream_epoch !== streamEpochRef.current
        ) {
          console.warn("[ws] event epoch mismatch; reconnecting");
          ws.close(4002, "stream epoch mismatch");
          return;
        }
        if (data.stream_epoch) streamEpochRef.current = data.stream_epoch;
        if (typeof data.seq === "number" && data.seq > 0) {
          if (data.seq <= lastSeqRef.current) {
            return;
          }
          if (data.seq !== lastSeqRef.current + 1) {
            console.warn(
              `[ws] sequence gap ${lastSeqRef.current} → ${data.seq}; reconnecting`
            );
            ws.close(4003, "event sequence gap");
            return;
          }
          lastSeqRef.current = data.seq;
          persistCursor();
        }
        applyEvent(data);
      };

      ws.onerror = () => {
        /* close handler 会触发重连 */
      };

      ws.onclose = (event) => {
        stateRef.current = "closed";
        setConnected(false);
        stopWatchdog();
        if (stopRef.current) return;
        if (event.code !== 4401) {
          scheduleReconnect();
          return;
        }
        void (async () => {
          try {
            await ensureServerSession(true);
            if (stopRef.current || authBlockedRef.current) return;
            retryRef.current = 0;
            scheduleReconnect(0);
          } catch (error) {
            if (isIdentityLostError(error)) {
              authBlockedRef.current = true;
              stopReconnectTimer();
              return;
            }
            scheduleReconnect();
          }
        })();
      };
    };

    const handleIdentityStatus = (event: Event): void => {
      const status = (event as CustomEvent<SessionIdentityStatus>).detail;
      if (
        status?.phase !== "ready" ||
        !authBlockedRef.current ||
        stopRef.current
      ) {
        return;
      }
      authBlockedRef.current = false;
      retryRef.current = 0;
      stopReconnectTimer();
      void connect();
    };

    window.addEventListener(SESSION_IDENTITY_EVENT, handleIdentityStatus);
    void connect();

    return () => {
      stopRef.current = true;
      window.removeEventListener(SESSION_IDENTITY_EVENT, handleIdentityStatus);
      stopWatchdog();
      stopReconnectTimer();
      wsRef.current?.close();
    };
  }, [setConnected, applyEvent]);
}

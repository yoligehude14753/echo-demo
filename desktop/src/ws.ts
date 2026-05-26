import { useEffect, useRef } from "react";
import { useStore } from "@/store";
import type { EchoEvent } from "@/types";

const WS_URL =
  typeof window !== "undefined" &&
  window.location.protocol.startsWith("http")
    ? `${window.location.protocol.replace("http", "ws")}//${window.location.host}/ws/echo`
    : "ws://localhost:5173/ws/echo";

export function useEchoWS(): void {
  const setConnected = useStore((s) => s.setConnected);
  const applyEvent = useStore((s) => s.applyEvent);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const stopRef = useRef(false);

  useEffect(() => {
    stopRef.current = false;
    const connect = (): void => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
      };
      ws.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data);
          if (data && typeof data.type === "string") {
            applyEvent(data as EchoEvent);
          }
        } catch {
          /* ignore */
        }
      };
      ws.onerror = () => {
        // 由 onclose 处理重连
      };
      ws.onclose = () => {
        setConnected(false);
        if (stopRef.current) return;
        retryRef.current = Math.min(retryRef.current + 1, 8);
        const backoff = Math.min(500 * 2 ** retryRef.current, 8_000);
        setTimeout(connect, backoff);
      };
    };
    connect();
    return () => {
      stopRef.current = true;
      wsRef.current?.close();
    };
  }, [setConnected, applyEvent]);
}

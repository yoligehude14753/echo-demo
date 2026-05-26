import { useEffect, useRef } from "react";
import { useStore } from "@/store";
import type { EchoEvent } from "@/types";
import { backendWsUrl } from "@/runtime";

export function useEchoWS(): void {
  const setConnected = useStore((s) => s.setConnected);
  const applyEvent = useStore((s) => s.applyEvent);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const stopRef = useRef(false);

  useEffect(() => {
    stopRef.current = false;

    const connect = async (): Promise<void> => {
      if (stopRef.current) return;
      const url = await backendWsUrl();
      const ws = new WebSocket(url);
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
        /* close 处理重连 */
      };
      ws.onclose = () => {
        setConnected(false);
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
      wsRef.current?.close();
    };
  }, [setConnected, applyEvent]);
}

/**
 * useTtsPlayer：EchoDesk TTS 主链路前端 hook。
 *
 * 行为：
 * - 暴露 ``speak(text)`` 给应用：fetch /tts/speak 拿 PCM 16-bit mono 16kHz → 用 AudioContext 播放
 * - 监听 WS 事件 ``tts.suggested`` → 当用户开关 ``ttsEnabled === true`` 时自动 speak()
 * - 顺序队列：同一时刻只播一段，新请求排队（避免多段重叠出现的噪声）
 * - 开关持久化到 ``localStorage("echodesk.tts.enabled")``，默认开启
 *
 * 设计上不依赖 ws.ts 的实现，只订阅 store.events（applyEvent 已经写完后会有更新）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ttsSpeak } from "@/api";
import { useStore } from "@/store";

const STORAGE_KEY = "echodesk.tts.enabled";
const SAMPLE_RATE = 16_000;

function loadEnabled(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === null) return true;
    return v === "1" || v === "true";
  } catch {
    return true;
  }
}

function persistEnabled(v: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, v ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function pcm16ToAudioBuffer(
  ctx: AudioContext,
  pcm: ArrayBuffer,
  sampleRate: number,
): AudioBuffer {
  const view = new DataView(pcm);
  const n = Math.floor(pcm.byteLength / 2);
  const buf = ctx.createBuffer(1, Math.max(n, 1), sampleRate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) {
    ch[i] = view.getInt16(i * 2, true) / 32768;
  }
  return buf;
}

export interface TtsController {
  enabled: boolean;
  setEnabled(v: boolean): void;
  speak(text: string): Promise<void>;
  cancel(): void;
  isSpeaking: boolean;
}

export function useTtsPlayer(): TtsController {
  const [enabled, setEnabledState] = useState<boolean>(loadEnabled);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const ctxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const queueRef = useRef<string[]>([]);
  const inflightRef = useRef(false);
  const lastSeqRef = useRef<number>(0);
  const events = useStore((s) => s.events);

  const ensureCtx = useCallback((): AudioContext => {
    if (!ctxRef.current) {
      ctxRef.current = new (window.AudioContext ||
        // @ts-expect-error: legacy WebKit alias
        window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
    }
    if (ctxRef.current.state === "suspended") {
      void ctxRef.current.resume();
    }
    return ctxRef.current;
  }, []);

  const playNow = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      try {
        const pcm = await ttsSpeak(trimmed);
        const ctx = ensureCtx();
        const buffer = pcm16ToAudioBuffer(ctx, pcm, SAMPLE_RATE);
        const src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        sourceRef.current = src;
        setIsSpeaking(true);
        await new Promise<void>((resolve) => {
          src.onended = () => resolve();
          src.start();
        });
      } catch (e) {
        console.warn("[tts] play failed", e);
      } finally {
        sourceRef.current = null;
        setIsSpeaking(false);
      }
    },
    [ensureCtx],
  );

  const drain = useCallback(async () => {
    if (inflightRef.current) return;
    inflightRef.current = true;
    try {
      while (queueRef.current.length > 0) {
        const next = queueRef.current.shift();
        if (!next) continue;
        await playNow(next);
      }
    } finally {
      inflightRef.current = false;
    }
  }, [playNow]);

  const speak = useCallback(
    async (text: string) => {
      if (!enabled) return;
      const t = text.trim();
      if (!t) return;
      queueRef.current.push(t);
      void drain();
    },
    [enabled, drain],
  );

  const cancel = useCallback(() => {
    queueRef.current = [];
    try {
      sourceRef.current?.stop();
    } catch {
      /* ignore */
    }
    sourceRef.current = null;
    setIsSpeaking(false);
  }, []);

  const setEnabled = useCallback(
    (v: boolean) => {
      setEnabledState(v);
      persistEnabled(v);
      if (!v) cancel();
    },
    [cancel],
  );

  // 监听 store.events 里的 tts.suggested → 自动播
  useEffect(() => {
    if (!enabled || events.length === 0) return;
    // 只看新事件（避免初次 mount 把历史全念一遍）
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      const seq = ev.seq || 0;
      if (seq <= lastSeqRef.current) break;
      if (ev.type === "tts.suggested") {
        const payload = ev.payload as { text?: string };
        if (payload?.text) {
          queueRef.current.push(payload.text);
        }
      }
    }
    if (events.length > 0) {
      lastSeqRef.current = Math.max(
        lastSeqRef.current,
        events[events.length - 1].seq || 0,
      );
    }
    void drain();
  }, [enabled, events, drain]);

  // 初次 mount 时把 lastSeqRef 调到当前最大，避免重放历史 tts.suggested
  useEffect(() => {
    const init = useStore.getState().events;
    if (init.length > 0) {
      lastSeqRef.current = init[init.length - 1].seq || 0;
    }
  }, []);

  return useMemo(
    () => ({ enabled, setEnabled, speak, cancel, isSpeaking }),
    [enabled, setEnabled, speak, cancel, isSpeaking],
  );
}

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
 *
 * phase4-tts 2026-05-28 加固（M_tts_check）：
 * - 失败一律 message.error 给用户人话——以前是 console.warn 静默吞掉，用户
 *   看到顶栏"TTS"绿灯但点啥都没声音，根因藏在 DevTools console 里
 * - 暴露 ``lastError`` / ``synthHealth``：顶栏 TTS 标签据此切红/黄色态
 * - 定期（30s）调 /tts/diag，再加 manual ``refreshHealth()`` 让 StatusBar
 *   popover 可以"立刻重试"
 */

import { message } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ttsDiag, ttsSpeak, TtsSpeakError, type TtsDiagResult } from "@/api";
import { useStore } from "@/store";

const STORAGE_KEY = "echodesk.tts.enabled";
const SAMPLE_RATE = 16_000;
const DIAG_POLL_INTERVAL_MS = 30_000;
// 同一条错误 30s 内只 toast 一次，避免 WS 连发多条 tts.suggested 时
// 用户被同样的 message.error 刷屏。
const ERROR_TOAST_DEDUPE_MS = 30_000;

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
  /** 最近一次失败的人话；播放成功后会清。null 表示当前健康。 */
  lastError: string | null;
  /** /tts/diag 最近一次结果；null 表示尚未拉过。 */
  synthHealth: TtsDiagResult | null;
  /** 手动强刷 /tts/diag（StatusBar popover 的"重试"按钮用）。 */
  refreshHealth(): Promise<void>;
}

export function useTtsPlayer(): TtsController {
  const [enabled, setEnabledState] = useState<boolean>(loadEnabled);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [synthHealth, setSynthHealth] = useState<TtsDiagResult | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const queueRef = useRef<string[]>([]);
  const inflightRef = useRef(false);
  const lastSeqRef = useRef<number>(0);
  const lastToastRef = useRef<{ msg: string; at: number } | null>(null);
  const events = useStore((s) => s.events);

  const reportError = useCallback((msg: string) => {
    setLastError(msg);
    const now = Date.now();
    const prev = lastToastRef.current;
    // 重复同一条 30s 内不再 toast；不同的错误（如先 silent 后 upstream）总是 toast。
    if (prev && prev.msg === msg && now - prev.at < ERROR_TOAST_DEDUPE_MS) return;
    lastToastRef.current = { msg, at: now };
    message.error(msg);
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      const r = await ttsDiag({ fresh: true });
      setSynthHealth(r);
      // 故意不在 r.ok 时清 lastError：lastError 描述的是"最近一次实际用户
      // 触发的 /tts/speak 失败"，diag 是独立 probe，两者解耦——只有真正的
      // 成功 playNow 才有资格清。否则 silent_output 偶发场景里，diag
      // probe 走运 ok 一次就把 toggle 又"洗绿"，用户报错的体感丢失。
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // /tts/diag 本身打不通 → 后端可能挂了；前端只 setLastError 不再 toast
      // （backend 健康有自己的 supervisor pill 显示）
      setLastError(`TTS 健康检查失败：${msg}`);
    }
  }, []);

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
        setLastError(null);
        await new Promise<void>((resolve) => {
          src.onended = () => resolve();
          src.start();
        });
      } catch (e) {
        // 关键修复：以前这里只 console.warn 静默吞掉，用户看到顶栏 TTS 绿灯
        // 却什么都没听到——以为整个 TTS 完全失效。现在走 message.error +
        // setLastError + 触发 health 刷新，让 StatusBar 同步变红。
        const msg =
          e instanceof TtsSpeakError
            ? e.message
            : e instanceof Error
              ? `TTS 播放失败：${e.message}`
              : `TTS 播放失败：${String(e)}`;
        console.warn("[tts] play failed", e);
        reportError(msg);
        // 后端报 silent_output / upstream_error 时立刻刷一次 diag，让 StatusBar
        // 不必等下一个 30s 轮询周期。
        void refreshHealth();
      } finally {
        sourceRef.current = null;
        setIsSpeaking(false);
      }
    },
    [ensureCtx, reportError, refreshHealth],
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

  // /tts/diag 定期轮询：第一次 mount 立刻拉，之后 30s 一轮。
  // 不依赖 enabled——即使用户关了 TTS，pill 也应该说"TTS 当前关闭"而不是
  // 看到陈旧的"ok"状态。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await ttsDiag();
        if (!cancelled) setSynthHealth(r);
      } catch {
        /* 静默：首拉失败时 lastError 留空，pill 显示 unknown 灰色 */
      }
    })();
    const id = window.setInterval(() => {
      void (async () => {
        try {
          const r = await ttsDiag();
          if (!cancelled) setSynthHealth(r);
        } catch {
          /* ignore */
        }
      })();
    }, DIAG_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return useMemo(
    () => ({
      enabled,
      setEnabled,
      speak,
      cancel,
      isSpeaking,
      lastError,
      synthHealth,
      refreshHealth,
    }),
    [enabled, setEnabled, speak, cancel, isSpeaking, lastError, synthHealth, refreshHealth],
  );
}

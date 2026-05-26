/**
 * MicRecorder：浏览器麦克风 → 16k mono PCM → 6s 一段切片 → POST /meetings/{id}/chunk
 *
 * 设计：
 * - 用 Web Audio API（AudioContext + ScriptProcessor 兜底；优先 AudioWorklet）抓 PCM
 * - 16kHz mono 16bit 直接送，避免 MediaRecorder 出 webm/opus 再转码的损耗
 * - 6 秒一段（与 backend pipeline chunk 长度匹配，不会让 STT 排队过长）
 * - 录音状态：idle / requesting / recording / error
 * - 一键开始即创建会议 + 录音；停止即 finalize
 */
import { useEffect, useRef, useState } from "react";
import { Button, Tag, message } from "antd";
import { Mic, MicOff, Loader2 } from "lucide-react";

import { startMeeting, uploadChunk, finalizeMeeting } from "@/api";
import { useStore } from "@/store";

const SAMPLE_RATE = 16_000;
const CHUNK_SECONDS = 6;
const CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_SECONDS;

type RecState = "idle" | "requesting" | "recording" | "stopping" | "error";

function floatTo16BitPCM(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function downsample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const newLength = Math.round(input.length / ratio);
  const out = new Float32Array(newLength);
  let inOff = 0;
  let outOff = 0;
  while (outOff < newLength) {
    const nextInOff = Math.round((outOff + 1) * ratio);
    let acc = 0;
    let cnt = 0;
    for (let i = inOff; i < nextInOff && i < input.length; i++) {
      acc += input[i];
      cnt++;
    }
    out[outOff] = cnt > 0 ? acc / cnt : 0;
    outOff++;
    inOff = nextInOff;
  }
  return out;
}

function pcm16ToWav(pcm: Int16Array, sampleRate: number): Blob {
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buf);
  const writeStr = (off: number, s: string): void => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + pcm.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
  return new Blob([buf], { type: "audio/wav" });
}

export default function MicRecorder(): JSX.Element {
  const [state, setState] = useState<RecState>("idle");
  const [chunkCount, setChunkCount] = useState(0);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const procRef = useRef<ScriptProcessorNode | null>(null);
  const bufRef = useRef<Float32Array[]>([]);
  const accSamplesRef = useRef(0);
  const meetingIdRef = useRef<string | null>(null);
  const selectMeeting = useStore((s) => s.selectMeeting);
  const upsertMeeting = useStore((s) => s.upsertMeeting);

  useEffect(() => {
    return () => {
      stopAll();
    };
  }, []);

  function stopAll(): void {
    procRef.current?.disconnect();
    procRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    audioCtxRef.current?.close().catch(() => undefined);
    audioCtxRef.current = null;
    bufRef.current = [];
    accSamplesRef.current = 0;
  }

  async function flushChunk(force = false): Promise<void> {
    if (!meetingIdRef.current) return;
    if (!force && accSamplesRef.current < CHUNK_SAMPLES) return;
    if (bufRef.current.length === 0) return;
    const total = bufRef.current.reduce((s, b) => s + b.length, 0);
    const merged = new Float32Array(total);
    let off = 0;
    for (const b of bufRef.current) {
      merged.set(b, off);
      off += b.length;
    }
    bufRef.current = [];
    accSamplesRef.current = 0;

    const ctx = audioCtxRef.current;
    if (!ctx) return;
    const down = downsample(merged, ctx.sampleRate, SAMPLE_RATE);
    const pcm = floatTo16BitPCM(down);
    const wav = pcm16ToWav(pcm, SAMPLE_RATE);
    try {
      const segs = await uploadChunk(meetingIdRef.current, wav, SAMPLE_RATE);
      setChunkCount((c) => c + 1);
      if (segs.length === 0) {
        // STT 没识别出，是空段，不打扰用户
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.warning(`chunk 上传失败：${msg}`);
    }
  }

  async function start(): Promise<void> {
    if (state === "recording" || state === "requesting") return;
    setState("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: SAMPLE_RATE, echoCancellation: true, noiseSuppression: true },
        video: false,
      });
      streamRef.current = stream;

      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const src = ctx.createMediaStreamSource(stream);
      // ScriptProcessor 在 Safari/旧浏览器更稳；AudioWorklet 更优但 demo 阶段够用
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      procRef.current = proc;

      proc.onaudioprocess = (ev) => {
        const ch = ev.inputBuffer.getChannelData(0);
        // 拷贝，因为 ScriptProcessor 复用 buffer
        bufRef.current.push(new Float32Array(ch));
        accSamplesRef.current += Math.round((ch.length * SAMPLE_RATE) / ctx.sampleRate);
        if (accSamplesRef.current >= CHUNK_SAMPLES) {
          void flushChunk();
        }
      };
      src.connect(proc);
      proc.connect(ctx.destination);

      const mid = `m-${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}`;
      meetingIdRef.current = mid;
      await startMeeting(mid);
      // 乐观更新 UI：不等 WS event 也立刻切到当前会议
      upsertMeeting(mid, { state: "in_meeting", started_at: new Date().toISOString() });
      selectMeeting(mid);
      setChunkCount(0);
      setState("recording");
      message.success(`已开始录音：${mid}`);
    } catch (e) {
      stopAll();
      meetingIdRef.current = null;
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`无法访问麦克风：${msg}`);
      setState("error");
    }
  }

  async function stop(): Promise<void> {
    if (state !== "recording") return;
    setState("stopping");
    try {
      await flushChunk(true);
      stopAll();
      const mid = meetingIdRef.current;
      meetingIdRef.current = null;
      if (mid) {
        message.info(`正在生成纪要…`);
        const minutes = await finalizeMeeting(mid, `会议 ${mid}`);
        message.success(`纪要已生成：${minutes.sections.length} 节 · ${minutes.decisions.length} 决议`);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`停止失败：${msg}`);
    } finally {
      setState("idle");
    }
  }

  if (state === "recording") {
    return (
      <div className="flex items-center gap-2">
        <Tag color="red" className="!m-0">
          <span className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
            录音中 · {chunkCount} chunk
          </span>
        </Tag>
        <Button
          size="small"
          danger
          icon={<MicOff className="w-3.5 h-3.5" />}
          onClick={() => void stop()}
        >
          停止
        </Button>
      </div>
    );
  }
  if (state === "stopping") {
    return (
      <Tag color="orange" icon={<Loader2 className="w-3 h-3 animate-spin" />}>
        正在生成纪要…
      </Tag>
    );
  }
  if (state === "requesting") {
    return (
      <Tag color="blue" icon={<Loader2 className="w-3 h-3 animate-spin" />}>
        请求麦克风…
      </Tag>
    );
  }
  return (
    <Button
      size="small"
      type="primary"
      icon={<Mic className="w-3.5 h-3.5" />}
      onClick={() => void start()}
    >
      开始录音
    </Button>
  );
}

/** PCM / WAV 工具（Capture 层，与 UI / 会议无关） */

export const CAPTURE_SAMPLE_RATE = 16_000;
// 4s 分块：比 6s 更快出 STT/回复（响应延迟 -33%）。跨 chunk 拆分由
// voiceWake 的"已唤醒窗口"兜底，所以缩短窗口不会丢唤醒指令。
// 仍 ≥3s，满足后端 cps 幻觉门与 min_speech_frame_ratio 的判定条件。
export const CAPTURE_CHUNK_SECONDS = 4;
export const CAPTURE_CHUNK_SAMPLES = CAPTURE_SAMPLE_RATE * CAPTURE_CHUNK_SECONDS;

export function floatTo16BitPCM(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

export function downsample(
  input: Float32Array,
  fromRate: number,
  toRate: number,
): Float32Array {
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

export function pcm16ToWav(pcm: Int16Array, sampleRate: number): Blob {
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

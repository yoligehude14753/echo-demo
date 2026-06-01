/**
 * TTS 句级流式合成器 —— 移植自老 echo pipeline 的「强分句 + 软分句」策略。
 *
 * 问题（用户反馈）：TTS 播放比文本输出晚太多。原实现等整段回答 onFinal 后才
 * 一次性合成全文，首句要等全文流完 + 合成完才出声。
 *
 * 解法：LLM token 流式到达时按句切片，逐句喂给 TTS 队列（useTtsPlayer 内部
 * 顺序播放）。这样首句一就绪就开播，后续句子边流边排队，几乎与文本同步。
 *
 * 分句规则（同老 echo）：
 * - 强分句：。！？!? 换行 —— 字数 ≥ 4 即切（避免"嗯。"单字成句）
 * - 软分句：，、；：,; —— 缓冲较长(≥16)时才在最后一个软标点切，尽早起播
 */
import type { TtsController } from "@/hooks/useTtsPlayer";
import { toSpeakableAnswer } from "@/lib/voiceWake";

const STRONG_END = /[。！？!?\n]/;
const SOFT_BREAK = /[，、；：,;:]/;
const MIN_STRONG_LEN = 4;
const SOFT_TRIGGER_LEN = 16;
const SOFT_MIN_LEN = 8;

export interface TtsSentenceStreamer {
  /** 喂入一段流式增量文本（delta）。 */
  push(delta: string): void;
  /** 流结束时调用：把剩余缓冲念完；若全程没念过且给了全文则念全文兜底。 */
  finalize(fullText?: string): void;
}

/** 找到当前缓冲里第一个可切句点，返回切分位置（exclusive）；无则 -1。 */
function cutIndex(text: string): number {
  for (let i = 0; i < text.length; i++) {
    if (STRONG_END.test(text[i]) && i + 1 >= MIN_STRONG_LEN) return i + 1;
  }
  if (text.length >= SOFT_TRIGGER_LEN) {
    for (let i = text.length - 1; i >= SOFT_MIN_LEN; i--) {
      if (SOFT_BREAK.test(text[i])) return i + 1;
    }
  }
  return -1;
}

/**
 * 为一次回答创建句级流式 TTS 合成器。
 *
 * 不在创建时锁定 enabled——而是每次发声时动态判断 ``tts.enabled``，避免"生成
 * 过程中用户刚打开 TTS"或"创建时机早于 enabled 生效"导致整轮静音。``tts.speak``
 * 内部也会再判一次 enabled，双保险。
 */
export function createTtsSentenceStreamer(tts: TtsController): TtsSentenceStreamer {
  let pending = "";
  let started = false;

  const speak = (raw: string): void => {
    if (!tts.enabled) return;
    const s = toSpeakableAnswer(raw).trim();
    if (!s) return;
    // 首句 interrupt 清空旧队列；后续句子顺序排队。
    void tts.speak(s, { interrupt: !started });
    started = true;
  };

  return {
    push(delta: string): void {
      pending += delta;
      let idx = cutIndex(pending);
      while (idx > 0) {
        speak(pending.slice(0, idx));
        pending = pending.slice(idx);
        idx = cutIndex(pending);
      }
    },
    finalize(fullText?: string): void {
      if (!started && !pending.trim() && fullText) {
        speak(fullText);
        return;
      }
      if (pending.trim()) speak(pending);
      pending = "";
    },
  };
}

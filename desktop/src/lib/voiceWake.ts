/**
 * 语音唤醒词匹配 —— 移植并扩展自老版本 echo 的「音节级模糊匹配」唤醒词库
 * （backend/app/nodes/router.py 的 _wake_match），单一真源。
 *
 * 背景（真链路实测）：真人说"Echo"经 FireRed/SenseVoice STT 后形态极不稳定：
 *   echo / aiko / pico / 诶口 / 爱扣 / 嘿依co / 汉宜口 / 汉语狗 ……
 * 穷举词表无法覆盖。老库的关键洞察：**Echo = 起头音节(可选) + Echo 音节**，
 * 而 Echo 音节 = 「第一音节(i/u/ai 韵谐音字) + 第二音节(-ko → 口/扣/狗…)」。
 * 把第一音节约束在 i/u/ai 韵谐音集，天然排除「开口/出口/门口/小狗/折扣」等
 * （首字发音不在集内），比维护大词表 blocklist 精准得多。
 *
 * 四层判定（任一命中即唤醒）：
 *  1. 强：起头音节(嗨/嘿/海/哎/诶/hey/hi) + Echo body —— 任意位置
 *  2. 中：句首(可吞 1 个乱码前缀) + 中文 Echo body —— 配极小 blocklist
 *  3. 英文：echo/echodesk/pico/aiko… —— 句首
 *  4. 归一化兜底：去标点空格后再跑 1-3 层（应对 STT 乱插标点）
 * 命中后再过 _wake_is_genuine 语义后过滤（排除"我跟echo说过"等提及）。
 */

// 起头音节（可选）：问候 / 口语应声常被前置到 Echo 前。
const HEAD = "(?:嗨|嘿|嘿哟|海|哎|诶|欸|嗳|喂|hey|hi)";

// Echo 第一音节谐音字（i 韵 / ai·e 韵 / u 韵）——实测 STT 输出谱：
//   i 韵：衣依伊一医易益意宜艺亿翼逸唉哎  ai/e 韵：爱艾埃诶欸  u 韵：鱼玉余于愚榆逾渝愉域裕育语雨与予羽宇寓誉
const ECHO_FIRST =
  "[衣依伊一医易益意宜艺亿翼逸唉哎爱艾埃诶欸鱼玉余于愚榆逾渝愉域裕育语雨与予羽宇寓誉]";
// Echo 第二音节 -ko 谐音尾字。
const ECHO_TAIL = "(?:扣|口|寇|叩|狗|苟|co儿|co)";
// 中文 Echo body：第一音节 + 尾字，中间容忍标点/空格。
const ECHO_CN = `${ECHO_FIRST}[\\s,，]*${ECHO_TAIL}`;
// 英文 / 拼音 body（高辨识度，正常对话极少出现）。
const ECHO_EN = "echo\\s*desk|echodesk|echoes|echo|ecko|aiko|aico|eiko|pico|iqoo|i\\s*qoo";
// 仅在带起头音节时才放行的歧义英文（eco/ego 单独出现是常见词）。
const ECHO_EN_LEAD_ONLY = "eco|ego|ico|iko";

// 唤醒词后必须是结尾 / 标点 / 汉字（避免词内误匹配，如 "echolocation"）。
const BOUNDARY_AFTER = "(?=$|[\\s,，。.!！?？:：、]|\\p{Script=Han})";
// 句首：字符串开头，或上一句结束标点之后。
const SENTENCE_START = "(?:^|[。.!！?？]\\s*)";

// 第 1 层 强：起头音节 + body（含歧义英文），任意位置。
const RE_STRONG = new RegExp(
  `${HEAD}[\\s,，]*(?:${ECHO_EN}|${ECHO_EN_LEAD_ONLY}|${ECHO_CN})${BOUNDARY_AFTER}`,
  "iu",
);
// 第 2 层 中：句首(可吞 1 个乱码前缀汉字，如"汉宜口"的"汉") + 中文 body；
// body 单独捕获用于极小 blocklist 复核。
const RE_CN_START = new RegExp(
  `${SENTENCE_START}[\\u4e00-\\u9fff]?(${ECHO_CN})${BOUNDARY_AFTER}`,
  "iu",
);
// 第 3 层 英文：句首英文/拼音 body。
const RE_EN_START = new RegExp(`${SENTENCE_START}(?:${ECHO_EN})${BOUNDARY_AFTER}`, "iu");

// 极小 blocklist：仅 i/u/ai 韵首字 + 口/扣/狗 仍可能撞上的常见真实词。
// （大量"开口/出口/门口/小狗"等首字不在谐音集，已被 ECHO_FIRST 天然排除。）
const FUZZY_BLOCKLIST = new Set([
  "一口", "一扣", "两口", "三口", "几口", "鱼口", "玉口", "余口",
]);

// 归一化：去掉所有标点 / 空白，让 body 匹配不受 STT 乱插标点影响。
const RE_PUNCT_SPACE = /[\s，。！？、,.!?;:'"（）()【】[\]…—\-_/\\]+/g;
function normalizeForWake(t: string): string {
  return t.toLowerCase().replace(RE_PUNCT_SPACE, "");
}

/** 在文本里找唤醒匹配。命中返回 {end:唤醒词结束位置}，否则 null。 */
function findWake(text: string): { end: number } | null {
  // 第 1 层 强：起头 + body
  const strong = RE_STRONG.exec(text);
  if (strong) return { end: strong.index + strong[0].length };
  // 第 3 层 英文句首（先于中文中层，英文更高置信）
  const en = RE_EN_START.exec(text);
  if (en) return { end: en.index + en[0].length };
  // 第 2 层 中文句首 body（配 blocklist）
  const cn = RE_CN_START.exec(text);
  if (cn) {
    const body = cn[1].replace(RE_PUNCT_SPACE, "");
    if (!FUZZY_BLOCKLIST.has(body)) return { end: cn.index + cn[0].length };
  }
  return null;
}

// ── 语义后过滤：排除「提及/自言自语提到 echo」（移植自老库 _wake_is_genuine）──
const ECHO_NAME = "(?:echo|衣扣|依扣|伊扣|一扣|哎扣|嘿依co|嘿依扣|宜口|语狗)";
const RE_MENTION_NEARBY = new RegExp(
  `(我跟|我和|我对|跟|和)\\s*${ECHO_NAME}\\s*(说|讲|聊|提|讨论|商量)`,
  "iu",
);
const RE_MENTION_ABOUT = new RegExp(
  `(提到|提起|说到|聊到|讲到|说过|讲过|关于)\\s*["']?${ECHO_NAME}`,
  "iu",
);

/** 命中唤醒词后，判断是否「真在对 Echo 说话」（排除提及/转述）。 */
function isGenuineWake(text: string): boolean {
  if (RE_MENTION_NEARBY.test(text)) return false;
  if (RE_MENTION_ABOUT.test(text)) return false;
  return true;
}

const SPEAKABLE_MAX_CHARS = 700;

/**
 * 从一段（STT）文本里抽取"Echo 唤醒后的指令"。命中返回指令文本，否则返回 null。
 * 唤醒词本身 + 前导问候 + 紧跟的标点会被剥掉，只留下用户真正的指令。
 */
export function extractEchoWakeCommand(raw: string): string | null {
  const text = raw.trim().replace(/\s+/g, " ");
  if (!text) return null;
  const hit = findWake(text);
  if (hit === null) return null;
  if (!isGenuineWake(text)) return null;
  const command = text
    .slice(hit.end)
    .replace(/^[\s,，。.!！?？:：、]+/, "")
    .trim();
  return command.length > 0 ? command : null;
}

/**
 * 仅判断文本里是否含唤醒词（不要求后面有指令）。
 *
 * 用于「跨 chunk 唤醒拼接」：固定分块可能把"echo"和后面的指令切到两个
 * chunk，此时 ``extractEchoWakeCommand`` 对第一个 chunk 返回 null（唤醒词后无
 * 指令）。调用方据此进入"已唤醒"窗口，把下一个 chunk 当作指令。
 *
 * 双跑：原文 + 去标点归一化文本，任一命中即可（应对 STT 乱插标点/空格）。
 */
export function containsWakeWord(raw: string): boolean {
  const text = raw.trim().replace(/\s+/g, " ");
  if (!text) return false;
  if (!isGenuineWake(text)) return false;
  if (findWake(text) !== null) return true;
  return findWake(normalizeForWake(text)) !== null;
}

/** 把 Markdown / 代码块答案转成适合 TTS 朗读的纯文本，并限制长度。 */
export function toSpeakableAnswer(raw: string): string {
  const cleaned = raw
    .replace(/\[doc:[^\]]+\]/g, "")
    .replace(/```[\s\S]*?```/g, "代码片段已生成，详情请看屏幕。")
    .replace(/[#>*_`|[\]]/g, "")
    .replace(/\[(.*?)\]\((.*?)\)/g, "$1")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (cleaned.length <= SPEAKABLE_MAX_CHARS) return cleaned;
  return `${cleaned.slice(0, SPEAKABLE_MAX_CHARS).trim()}。后面内容较长，已显示在屏幕上。`;
}

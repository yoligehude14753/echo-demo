/**
 * Speaker label 显示工具——前端唯一来源（避免计数与显示不同源）。
 *
 * 设计：
 * - 后端 SpeakerRegistry 给的是跨会议持久的全局 ID（"speaker_55" 之类），数字越来越大
 * - 用户在 UI 上只关心"这是说话人 1、2、3..."（按本视图首次出现的顺序）
 * - 所有展示 / 计数 / 颜色都基于本文件返回的 displayIdx → 一处定义、多处复用
 *
 * 为什么要抽到 lib：在此之前 TranscriptStream 自己 remap 出 47，
 * MeetingList 直接读 store.speakers.size 得到 86 → 两个数字对不上（用户截图）。
 * 修法：MeetingList 也用本工具按 segments 算，与转写流的最大编号一致。
 */

export interface HasSpeakerLabel {
  speaker_label?: string | null;
}

export function buildSpeakerDisplayMap<T extends HasSpeakerLabel>(
  segs: readonly T[],
): Map<string, number> {
  const m = new Map<string, number>();
  let next = 1;
  for (const s of segs) {
    const raw = s.speaker_label;
    if (!raw) continue;
    if (!m.has(raw)) {
      m.set(raw, next);
      next += 1;
    }
  }
  return m;
}

/** 返回本组 segments 中的显示用 speaker 数。 */
export function countDisplaySpeakers<T extends HasSpeakerLabel>(
  segs: readonly T[],
): number {
  return buildSpeakerDisplayMap(segs).size;
}

/** 同一调色板供 transcript / 头像 / list 用（保证视觉一致）。 */
export const SPEAKER_COLORS: readonly { fg: string; bg: string; ring: string }[] =
  [
    { fg: "#10a37f", bg: "#ecfdf5", ring: "#a7f3d0" },
    { fg: "#2563eb", bg: "#eff6ff", ring: "#bfdbfe" },
    { fg: "#d97706", bg: "#fffbeb", ring: "#fde68a" },
    { fg: "#db2777", bg: "#fdf2f8", ring: "#fbcfe8" },
    { fg: "#7c3aed", bg: "#f5f3ff", ring: "#ddd6fe" },
    { fg: "#0891b2", bg: "#ecfeff", ring: "#a5f3fc" },
    { fg: "#65a30d", bg: "#f7fee7", ring: "#d9f99d" },
  ];

export function colorForDisplayIdx(
  idx: number,
): { fg: string; bg: string; ring: string } {
  if (idx <= 0)
    return { fg: "#737373", bg: "#f5f5f5", ring: "#e5e5e5" };
  return SPEAKER_COLORS[(idx - 1) % SPEAKER_COLORS.length];
}

import type { ArtifactKind } from "@/api";

export interface ExplicitArtifactCommand {
  artifactType: ArtifactKind;
  brief: string;
}

// 生成类动词（出现在句中任意位置即可，不要求句首）。用具体短语避免裸"写/做"
// 过度触发（"做关系""写代码注释"等）。
const GENERATE_VERBS = [
  "生成",
  "创建",
  "制作",
  "输出",
  "撰写",
  "帮我写",
  "帮我做",
  "帮我生成",
  "帮你写",
  "写一份",
  "写一个",
  "写一篇",
  "写一张",
  "做一份",
  "做一个",
  "做一篇",
  "做一张",
  "做个",
  "写个",
  "出一份",
  "出一个",
  "做成",
  "整理成",
  "整成",
  "弄成",
  "汇总成",
  "总结成",
];

// 产物类型关键词（按优先级 / 出现位置取最靠前的一个）。
const ARTIFACT_KEYWORDS: Array<[string, ArtifactKind]> = [
  ["html", "html"],
  ["网页", "html"],
  ["pptx", "pptx"],
  ["ppt", "pptx"],
  ["幻灯片", "pptx"],
  ["演示文稿", "pptx"],
  ["deck", "pptx"],
  ["excel", "xlsx"],
  ["xlsx", "xlsx"],
  ["电子表格", "xlsx"],
  ["表格", "xlsx"],
  ["word", "word"],
  ["docx", "word"],
  ["文档", "word"],
  ["报告", "word"],
  ["方案书", "word"],
  ["markdown", "markdown"],
  ["pdf", "pdf"],
];

// 需要联网/检索的信号：命中则不走"直接生成"，交给 agent 先调研再生成。
const RESEARCH_SIGNALS = [
  "调研",
  "联网",
  "搜索",
  "搜一下",
  "查一下",
  "查询",
  "最新新闻",
  "实时",
  "今天的新闻",
];

function normalizeCommand(raw: string): string {
  return raw
    .trim()
    .replace(/^@(?:echo|echodesk)\s*/i, "")
    .replace(/^@/, "")
    .trim();
}

/** 在文本里找最靠前出现的产物关键词，返回其类型；找不到返回 null。 */
function firstArtifactType(text: string): ArtifactKind | null {
  const lower = text.toLowerCase();
  let bestIdx = Infinity;
  let bestType: ArtifactKind | null = null;
  for (const [kw, type] of ARTIFACT_KEYWORDS) {
    const idx = lower.indexOf(kw);
    if (idx >= 0 && idx < bestIdx) {
      bestIdx = idx;
      bestType = type;
    }
  }
  return bestType;
}

/**
 * 识别"明确要生成某类产物"的指令，命中则走**直接生成**（跳过脆弱的 agent 编排）。
 *
 * 放宽规则（2026-06 修复"请生成…"类指令一直走 agent 导致生成不出来）：
 * - 生成动词可在句中任意位置（"请生成…""帮我写一份…""做一个…"均可）
 * - 产物名词可在句中任意位置，取最靠前的一个作为类型
 * - 含"调研/搜索/最新"等联网信号时返回 null，交给 agent 先检索再生成
 */
export function extractExplicitArtifactCommand(
  raw: string,
): ExplicitArtifactCommand | null {
  const normalized = normalizeCommand(raw);
  if (!normalized) return null;
  const lower = normalized.toLowerCase();

  const hasVerb = GENERATE_VERBS.some((v) => normalized.includes(v));
  if (!hasVerb) return null;

  // 需要联网调研的复合任务交给 agent（先 search 再 generate）。
  if (RESEARCH_SIGNALS.some((s) => lower.includes(s))) return null;

  const artifactType = firstArtifactType(normalized);
  if (!artifactType) return null;

  return { artifactType, brief: normalized };
}

/**
 * 失败产物（FailedArtifact）—— 纯 frontend 概念。
 *
 * 背景：后端 `artifact.failed` 事件 payload 只携带最小信息（artifact_type + error 截断 300 字符），
 * 不会产生 artifact_id（因为根本没生成出文件）。前端把这种事件转成一个内部展示模型，渲染到
 * ArtifactPanel 顶部的"失败卡片"。
 *
 * 不写进 types.ts：那是 WS 协议契约的全局类型，FailedArtifact 是 store 的本地视图模型。
 *
 * 实际 payload 字段以 backend/app/api/artifacts.py 为准：
 *   {"artifact_type": "html" | "pptx" | ..., "error": str(e)[:300]}
 *
 * intent_text 不在 payload 里，是 store 在收到上一条 artifact.generating 时把 brief 暂存
 * 起来，与 artifact.failed 配对后回填进来（同一会话内 best-effort 关联，找不到则置空）。
 */

import type { EchoEvent } from "@/types";

export interface FailedArtifact {
  /** frontend 生成的唯一 id（用于 React key 与 dismiss）。 */
  id: string;
  artifact_type: string;
  /** 用户原始命令（best-effort，从 artifact.generating 关联得来）。 */
  intent_text?: string;
  /** 错误原因，对应 backend payload.error。 */
  reason: string;
  /** ISO 时间戳，优先用 event.ts，缺失则 fallback 到 Date.now()。 */
  failed_at: string;
  meeting_id?: string | null;
  todo_id?: string | null;
  run_id?: string | null;
}

const FALLBACK_REASON = "未知错误";

interface ArtifactFailedPayload {
  artifact_type?: string;
  error?: string;
  todo_id?: string | null;
  run_id?: string | null;
}

/**
 * 把后端 artifact.failed 事件 + 可选的 intent_text 关联组装成 FailedArtifact。
 * 容错：payload 字段缺失时使用 fallback，避免一条坏事件让整个面板崩。
 */
export function buildFailedArtifact(
  event: EchoEvent,
  intentText: string | undefined,
): FailedArtifact {
  const payload = (event.payload ?? {}) as ArtifactFailedPayload;
  const artifactType =
    typeof payload.artifact_type === "string" && payload.artifact_type
      ? payload.artifact_type
      : "unknown";
  const reason =
    typeof payload.error === "string" && payload.error.trim()
      ? payload.error.trim()
      : FALLBACK_REASON;
  const failedAt =
    typeof event.ts === "string" && event.ts ? event.ts : new Date().toISOString();
  return {
    id: makeFailedId(failedAt),
    artifact_type: artifactType,
    intent_text: intentText && intentText.trim() ? intentText.trim() : undefined,
    reason,
    failed_at: failedAt,
    meeting_id: event.meeting_id ?? null,
    todo_id:
      typeof payload.todo_id === "string" && payload.todo_id
        ? payload.todo_id
        : null,
    run_id:
      typeof payload.run_id === "string" && payload.run_id
        ? payload.run_id
        : null,
  };
}

function makeFailedId(seed: string): string {
  // 用时间戳 + 随机后缀，足以在前端列表里去重；不必加密强度。
  const rand = Math.random().toString(36).slice(2, 8);
  return `failed-${seed}-${rand}`;
}

/**
 * 把 ISO 时间戳格式化为中文相对时间。
 * 规则：
 *   < 60s   → "刚才"
 *   < 60min → "N 分钟前"
 *   < 24h   → "N 小时前"
 *   else    → "M-D HH:mm"
 *
 * 显式接受 now 参数便于测试；默认 Date.now()。
 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diffSec = Math.max(0, Math.floor((now - t) / 1000));
  if (diffSec < 60) return "刚才";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour} 小时前`;
  const d = new Date(t);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

/** 失败卡片在 store 里的保留条数上限。 */
export const FAILED_ARTIFACT_LIMIT = 20;

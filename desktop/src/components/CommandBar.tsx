/**
 * CommandBar：底部输入条 + @意图路由器
 *
 * 行为：
 * 1. 用户输入文本，回车提交
 * 2. 调 POST /intent/route → 后端 LLM/关键字分类返回 IntentResult
 * 3. 按 kind 分发：
 *    - generate_{html,pptx,xlsx,word} → POST /artifacts/generate（沿用 ArtifactPanel 的 API）
 *    - summarize_meeting → POST /meetings/{id}/finalize
 *    - start_meeting     → POST /meetings/{id}/start
 *    - search_web / search_rag → POST /rag/ask 并把答案塞进 events 流
 *    - chat → 仅展示提示，不真正接入流式（MVP）
 *
 * UI：
 *   一个紧凑的悬浮输入条；显示当前意图徽标 / 解析理由。
 */

import { useState } from "react";
import { Input, Tag, Tooltip, message } from "antd";
import { Sparkles, Wand2, Loader2 } from "lucide-react";

import {
  finalizeMeeting,
  generateArtifact,
  ragAsk,
  routeIntent,
  startMeeting,
} from "@/api";
import { useStore } from "@/store";
import type { IntentKind, IntentResult } from "@/types";

const kindLabel: Record<IntentKind, string> = {
  search_web: "联网搜索",
  search_rag: "回忆历史",
  generate_html: "生成 HTML",
  generate_pptx: "生成 PPT",
  generate_xlsx: "生成 Excel",
  generate_word: "生成 Word",
  summarize_meeting: "总结会议",
  start_meeting: "开始会议",
  chat: "对话",
};

const kindColor: Record<IntentKind, string> = {
  search_web: "blue",
  search_rag: "purple",
  generate_html: "magenta",
  generate_pptx: "gold",
  generate_xlsx: "green",
  generate_word: "cyan",
  summarize_meeting: "geekblue",
  start_meeting: "lime",
  chat: "default",
};

export default function CommandBar(): JSX.Element {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [lastIntent, setLastIntent] = useState<IntentResult | null>(null);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const addArtifact = useStore((s) => s.addArtifact);
  const applyEvent = useStore((s) => s.applyEvent);

  async function onSubmit(): Promise<void> {
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    try {
      const r = await routeIntent(value, currentMeetingId);
      setLastIntent(r);
      await dispatch(r, value);
      setText("");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`意图路由失败：${msg}`);
    } finally {
      setBusy(false);
    }
  }

  async function dispatch(r: IntentResult, originalText: string): Promise<void> {
    switch (r.kind) {
      case "generate_html":
      case "generate_pptx":
      case "generate_xlsx":
      case "generate_word": {
        const kind = (r.params.artifact_type as string | undefined) ?? "html";
        const brief = (r.params.brief as string | undefined) ?? originalText;
        if (!brief) {
          message.warning("brief 为空，无法生成产物");
          return;
        }
        message.info(`已派发：${kindLabel[r.kind]}（后台生成中，请稍候）`);
        // 异步触发，结果通过 WS artifact.ready event 反馈到 store
        // 不 await，避免 busy/textarea 在 60-180s LLM 链路上一直锁住
        void generateArtifact({
          artifact_type: kind as "html" | "pptx" | "xlsx" | "word",
          brief,
        })
          .then((art) => {
            addArtifact(art);
            message.success(`已生成 ${art.artifact_type}`);
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            message.error(`生成失败：${msg}`);
          });
        return;
      }
      case "summarize_meeting": {
        const mid = (r.params.meeting_id as string | undefined) ?? currentMeetingId;
        if (!mid) {
          message.warning("当前没有选中的会议");
          return;
        }
        message.info(`正在总结会议 ${mid}…`);
        const minutes = await finalizeMeeting(mid, `会议 ${mid}`);
        message.success(`会议纪要已生成，共 ${minutes.sections.length} 节`);
        return;
      }
      case "start_meeting": {
        const mid =
          (r.params.meeting_id as string | undefined) ||
          `m-${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}`;
        await startMeeting(mid);
        message.success(`会议 ${mid} 已开启`);
        return;
      }
      case "search_web":
      case "search_rag": {
        const question = (r.params.question as string | undefined) ?? originalText;
        if (!question) {
          message.warning("question 为空");
          return;
        }
        message.info("已派发：检索中（后台进行中）");
        // 同样异步触发
        void ragAsk(question)
          .then((ans) => {
            applyEvent({
              type: "rag.answer.done",
              seq: 0,
              ts: new Date().toISOString(),
              payload: {
                question,
                answer: ans.answer,
                citations: ans.citations,
                arbitration: ans.arbitration,
              } as unknown as Record<string, unknown>,
            });
            message.success("已返回检索结果（见事件流）");
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            message.error(`检索失败：${msg}`);
          });
        return;
      }
      case "chat":
      default:
        message.info(
          `chat 兜底：${(r.params.text as string | undefined) ?? originalText}`,
        );
    }
  }

  return (
    <div className="border-t border-paper-300 bg-paper-100 px-4 py-2">
      {lastIntent && (
        <div className="flex items-center gap-2 mb-1.5 text-[11px] text-ink-500">
          <Sparkles className="w-3 h-3" />
          <span>意图：</span>
          <Tag color={kindColor[lastIntent.kind]} className="!m-0">
            {kindLabel[lastIntent.kind]}
          </Tag>
          <span>·</span>
          <span>
            置信度 {(lastIntent.confidence * 100).toFixed(0)}%
          </span>
          {lastIntent.rationale && (
            <Tooltip title={lastIntent.rationale}>
              <span className="truncate max-w-[280px]">
                · {lastIntent.rationale}
              </span>
            </Tooltip>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Wand2 className="w-4 h-4 text-ink-500 shrink-0" />
        <Input.TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          placeholder="试试：@生成 PPT 英伟达 2025 投资展望；或 @查 最新黄金价格；Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={busy}
          className="!rounded-md"
        />
        {busy && <Loader2 className="w-4 h-4 text-ink-400 animate-spin" />}
      </div>
    </div>
  );
}

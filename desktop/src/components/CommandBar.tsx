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

import { useCallback, useRef, useState } from "react";
import { Input, Tag, Tooltip, message } from "antd";
import { FileText, Loader2, Paperclip, Sparkles, Upload, Wand2, X } from "lucide-react";

import {
  finalizeMeeting,
  generateArtifact,
  ingestFile,
  ragAsk,
  routeIntent,
} from "@/api";
import { useStore } from "@/store";
import type { IntentKind, IntentResult } from "@/types";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";

interface PendingDoc {
  doc_id: string;
  title: string;
  filename: string;
}

const kindLabel: Record<IntentKind, string> = {
  search_web: "联网搜索",
  search_rag: "回忆历史",
  generate_html: "生成 HTML",
  generate_pptx: "生成 PPT",
  generate_xlsx: "生成 Excel",
  generate_word: "生成 Word",
  summarize_meeting: "总结会议",
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
  chat: "default",
};

// 与后端 parsers.SUPPORTED_EXTS 保持一致的子集（最常用的；其他扩展名走 markitdown 也支持）
const ACCEPT_EXT =
  ".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.csv,.epub,.msg,.eml," +
  ".md,.markdown,.txt,.text,.log,.rst,.json,.jsonl,.yaml,.yml,.xml,.srt,.vtt,.sql," +
  ".py,.js,.jsx,.ts,.tsx,.go,.rs,.java,.c,.cc,.cpp,.h,.hpp,.sh,.zsh,.toml,.ini,.cfg,.env,.conf";

const ACCEPT_EXT_SET = new Set(
  ACCEPT_EXT.split(",").map((s) => s.trim().toLowerCase()),
);

function pickExt(filename: string): string {
  const i = filename.lastIndexOf(".");
  return i >= 0 ? filename.slice(i).toLowerCase() : "";
}

export default function CommandBar(): JSX.Element {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(0);
  const [dropActive, setDropActive] = useState(false);
  const [pendingDocs, setPendingDocs] = useState<PendingDoc[]>([]);
  const [lastIntent, setLastIntent] = useState<IntentResult | null>(null);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const addArtifact = useStore((s) => s.addArtifact);
  const applyEvent = useStore((s) => s.applyEvent);
  const tts = useTtsPlayer();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const handleFiles = useCallback(async (files: FileList | File[]): Promise<void> => {
    const arr = Array.from(files);
    if (!arr.length) return;
    for (const f of arr) {
      const ext = pickExt(f.name);
      if (!ACCEPT_EXT_SET.has(ext)) {
        message.warning(`不支持的文件类型：${f.name}（${ext || "无后缀"}）`);
        continue;
      }
      setUploading((n) => n + 1);
      try {
        const r = await ingestFile(f, f.name);
        setPendingDocs((prev) => [
          ...prev,
          { doc_id: r.doc_id, title: r.title, filename: f.name },
        ]);
        message.success(`已入库：${f.name}`);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        message.error(`${f.name} 入库失败：${msg}`);
      } finally {
        setUploading((n) => Math.max(0, n - 1));
      }
    }
  }, []);

  const onDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files")) {
      setDropActive(true);
    }
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDropActive(false);
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setDropActive(false);
      if (e.dataTransfer?.files?.length) {
        void handleFiles(e.dataTransfer.files);
      }
    },
    [handleFiles],
  );

  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files: File[] = [];
      for (const it of items) {
        if (it.kind === "file") {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        void handleFiles(files);
      }
    },
    [handleFiles],
  );

  const removePendingDoc = useCallback((docId: string) => {
    setPendingDocs((prev) => prev.filter((d) => d.doc_id !== docId));
  }, []);

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
          message.warning("当前没有进行中的会议");
          return;
        }
        message.info(`正在总结会议 ${mid}…`);
        const minutes = await finalizeMeeting(mid, `会议 ${mid}`);
        message.success(`会议纪要已生成，共 ${minutes.sections.length} 节`);
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
            void tts.speak(ans.answer);
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            message.error(`检索失败：${msg}`);
          });
        return;
      }
      case "chat":
      default: {
        const reply = (r.params.text as string | undefined) ?? originalText;
        message.info(`chat 兜底：${reply}`);
        void tts.speak(reply);
      }
    }
  }

  return (
    <div
      className={`relative border-t border-paper-300 bg-paper-100 px-4 py-2 transition ${
        dropActive ? "ring-2 ring-inset ring-blue-400 bg-blue-50/50" : ""
      }`}
      onDragOver={onDragOver}
      onDragEnter={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      data-testid="command-bar"
    >
      {dropActive && (
        <div className="absolute inset-2 z-10 flex items-center justify-center rounded-md border-2 border-dashed border-blue-400 bg-white/95 text-sm text-blue-700 pointer-events-none">
          <Upload className="w-4 h-4 mr-2" />
          松手即可入库到 RAG（PDF / Word / Excel / PPT / md / txt / html / csv …）
        </div>
      )}

      {lastIntent && (
        <div className="flex items-center gap-2 mb-1.5 text-[11px] text-ink-500">
          <Sparkles className="w-3 h-3" />
          <span>意图：</span>
          <Tag color={kindColor[lastIntent.kind]} className="!m-0">
            {kindLabel[lastIntent.kind]}
          </Tag>
          <span>·</span>
          <span>置信度 {(lastIntent.confidence * 100).toFixed(0)}%</span>
          {lastIntent.rationale && (
            <Tooltip title={lastIntent.rationale}>
              <span className="truncate max-w-[280px]">
                · {lastIntent.rationale}
              </span>
            </Tooltip>
          )}
        </div>
      )}

      {(pendingDocs.length > 0 || uploading > 0) && (
        <div
          className="flex flex-wrap items-center gap-1.5 mb-1.5"
          data-testid="pending-docs"
        >
          {pendingDocs.map((d) => (
            <Tag
              key={d.doc_id}
              closable
              onClose={(e) => {
                e.preventDefault();
                removePendingDoc(d.doc_id);
              }}
              icon={<FileText className="w-3 h-3 inline -mt-0.5 mr-1" />}
              color="processing"
              className="!m-0 max-w-[200px] truncate"
              title={`${d.filename} → ${d.doc_id}`}
            >
              {d.filename}
            </Tag>
          ))}
          {uploading > 0 && (
            <span className="inline-flex items-center gap-1 text-[11px] text-ink-500">
              <Loader2 className="w-3 h-3 animate-spin" />
              入库中 {uploading}…
            </span>
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
          onPaste={onPaste}
          placeholder="拖入 / 粘贴文件入库 RAG · 输入 @生成 PPT … / @查 … · Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={busy}
          className="!rounded-md"
          data-testid="command-textarea"
        />
        <Tooltip title="选择文件入库 RAG（多选可批量）">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="shrink-0 p-1.5 rounded hover:bg-paper-200 text-ink-500 disabled:opacity-50"
            disabled={uploading > 0}
            data-testid="command-attach-btn"
            aria-label="上传文件"
          >
            <Paperclip className="w-4 h-4" />
          </button>
        </Tooltip>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ACCEPT_EXT}
          className="hidden"
          data-testid="command-file-input"
          onChange={(e) => {
            if (e.target.files?.length) {
              void handleFiles(e.target.files);
              e.target.value = "";
            }
          }}
        />
        {busy && <Loader2 className="w-4 h-4 text-ink-400 animate-spin" />}
      </div>
    </div>
  );
}

// 占位防止未来扩展时 lint 警告（X 用于 chip 关闭按钮的扩展位）
void X;

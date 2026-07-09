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
 *   一个紧凑的悬浮输入条。路由细节只用于内部派发，不暴露给普通用户。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Input, Tag, Tooltip, message } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { FileText, Loader2, Paperclip, Send, Upload, Wand2, X } from "lucide-react";

import {
  artifactDownloadUrl,
  chatAsk,
  createAgentTask,
  finalizeMeeting,
  generateArtifact,
  ingestFile,
  listRagDocs,
  ragAsk,
  routeIntent,
  workspaceStatus,
} from "@/api";
import { useStore, type CommandBarPrefillMeta } from "@/store";
import type { IntentKind, IntentResult } from "@/types";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";
import { isTvLikeViewport } from "@/runtime";

interface PendingDoc {
  doc_id: string;
  title: string;
  filename: string;
}

const kindLabel: Record<IntentKind, string> = {
  search_web: "联网搜索",
  search_rag: "查知识库",
  generate_html: "生成 HTML",
  generate_pptx: "生成 PPT",
  generate_xlsx: "生成 Excel",
  generate_word: "生成 Word",
  generate_markdown: "生成 Markdown",
  generate_pdf: "生成 PDF",
  generate_txt: "生成 TXT",
  summarize_meeting: "总结会议",
  agent_task: "后台任务",
  chat_no_rag: "对话",
  chat: "对话",
};

function routeExplicitGenerateCommand(value: string): IntentResult | null {
  const match = value.match(
    /@\s*生成\s*(pptx?|幻灯片|html|网页|页面|word|docx|文档|excel|xlsx|表格|markdown|md|pdf|txt|文本)/i,
  );
  const rawType = match?.[1]?.toLowerCase();
  if (!rawType) return null;

  const artifactType =
    rawType === "ppt" || rawType === "pptx" || rawType === "幻灯片"
      ? "pptx"
      : rawType === "html" || rawType === "网页" || rawType === "页面"
        ? "html"
        : rawType === "word" || rawType === "docx" || rawType === "文档"
          ? "word"
          : rawType === "excel" || rawType === "xlsx" || rawType === "表格"
            ? "xlsx"
            : rawType === "markdown" || rawType === "md"
              ? "markdown"
              : rawType === "pdf"
                ? "pdf"
                : "txt";

  return {
    kind: `generate_${artifactType}` as IntentKind,
    confidence: null,
    params: {
      artifact_type: artifactType,
      brief: value,
    },
    rationale: "explicit @生成 command",
  };
}

// 与后端 parsers.SUPPORTED_EXTS 保持一致的子集（最常用的；其他扩展名走 markitdown 也支持）
const ACCEPT_EXT =
  ".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.csv,.epub,.msg,.eml," +
  ".md,.markdown,.txt,.text,.log,.rst,.json,.jsonl,.yaml,.yml,.xml,.srt,.vtt,.sql," +
  ".py,.js,.jsx,.ts,.tsx,.go,.rs,.java,.c,.cc,.cpp,.h,.hpp,.sh,.zsh,.toml,.ini,.cfg,.env,.conf";

const ACCEPT_EXT_SET = new Set(
  ACCEPT_EXT.split(",").map((s) => s.trim().toLowerCase()),
);

function detectTvCommandMode(): boolean {
  if (
    typeof document !== "undefined" &&
    document.documentElement.classList.contains("echodesk-tv")
  ) {
    return true;
  }
  return isTvLikeViewport();
}

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
  const [prefillMeta, setPrefillMeta] = useState<CommandBarPrefillMeta | null>(null);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const addArtifact = useStore((s) => s.addArtifact);
  const applyEvent = useStore((s) => s.applyEvent);
  const upsertAgentTask = useStore((s) => s.upsertAgentTask);
  const registerCommandBarPrefill = useStore((s) => s.registerCommandBarPrefill);
  const tts = useTtsPlayer();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<TextAreaRef | null>(null);
  const [tvMode, setTvMode] = useState(() => detectTvCommandMode());
  const commandPlaceholder = tvMode
    ? "输入指令，如 @总结会议"
    : "输入问题 / @生成 PPT / @查 · 可拖入文件";
  const quickCommands = [
    { label: "总结会议", command: "@总结会议" },
    { label: "现在状态", command: "@chat 现在状态" },
    { label: "会议要点", command: "@查 当前会议要点" },
  ];

  useEffect(() => {
    const updateTvMode = () => setTvMode(detectTvCommandMode());
    updateTvMode();
    const timer = window.setTimeout(updateTvMode, 0);
    const raf = window.requestAnimationFrame(updateTvMode);
    const observer =
      typeof MutationObserver !== "undefined"
        ? new MutationObserver(updateTvMode)
        : null;
    observer?.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    window.addEventListener("resize", updateTvMode, { passive: true });
    window.addEventListener("orientationchange", updateTvMode, { passive: true });
    return () => {
      window.clearTimeout(timer);
      window.cancelAnimationFrame(raf);
      observer?.disconnect();
      window.removeEventListener("resize", updateTvMode);
      window.removeEventListener("orientationchange", updateTvMode);
    };
  }, []);

  // M_minutes_refactor：MinutesView「执行待办」按钮通过 store.prefillCommandBar
  // 把 suggested_command 一键填入；只 setText + focus，不自动 onSubmit 防误触。
  useEffect(() => {
    const unregister = registerCommandBarPrefill((nextText, meta) => {
      setText(nextText);
      setPrefillMeta(meta ?? null);
      textareaRef.current?.focus({ cursor: "end" });
    });
    return unregister;
  }, [registerCommandBarPrefill]);

  // P4-fix-rag-chat 智能提示节流：每条会话最多提示一次，避免每次发问都弹。
  const workspaceHintShownRef = useRef(false);

  // 当用户走 search_rag 但 RAG docs 数太少（< 3）且没配置 workspace_dirs 时，
  // toast 提示「📂 想覆盖整个文件夹？点 设置 → 工作区目录 配置」。
  const maybePromptWorkspaceConfig = useCallback(async (): Promise<void> => {
    if (workspaceHintShownRef.current) return;
    try {
      const [docs, ws] = await Promise.all([
        listRagDocs(),
        workspaceStatus(),
      ]);
      const nDocs = docs.total ?? 0;
      const nConfigured = ws.configured_dirs?.length ?? 0;
      if (nDocs < 3 && nConfigured === 0) {
        workspaceHintShownRef.current = true;
        message.info({
          content:
            "📂 想覆盖整个文件夹？点 设置（齿轮） → 工作区目录，加一个 ~/Documents 之类的目录，EchoDesk 会自动扫描索引",
          duration: 8,
        });
      }
    } catch {
      /* 静默：智能提示不应阻塞用户主问答 */
    }
  }, []);

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

  // P4-fix（2026-05-28）：附件已附 + 文本空 时按 Enter / 点 Send，
  // 历史上 onSubmit 静默 return，导致用户报"对话无法发送"。
  // 修复：附件存在时用默认 brief "请总结附件内容" 发送（让 dispatch 能跑下去），
  // 真正空（无文本无附件）才忽略；上传中仍然阻塞防 RAG 半成品。
  function canSubmit(): boolean {
    if (busy) return false;
    if (uploading > 0) return false;
    if (text.trim().length > 0) return true;
    if (pendingDocs.length > 0) return true;
    return false;
  }

  async function submitValue(
    value: string,
    activePrefillMeta: CommandBarPrefillMeta | null,
    clearPendingAfterSubmit: boolean,
  ): Promise<void> {
    if (!value) return;
    setBusy(true);
    try {
      const r = routeExplicitGenerateCommand(value) ?? (await routeIntent(value, currentMeetingId));
      await dispatch(r, value, activePrefillMeta);
      setText("");
      setPrefillMeta(null);
      // 附件已被发出，清空 pendingDocs（后端 RAG 检索复用 doc_id）
      if (clearPendingAfterSubmit) {
        setPendingDocs([]);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(`发送失败：${msg}`);
    } finally {
      setBusy(false);
    }
  }

  async function onSubmit(): Promise<void> {
    if (busy || uploading > 0) return;
    const trimmed = text.trim();
    const value =
      trimmed.length > 0
        ? trimmed
        : pendingDocs.length > 0
          ? `请基于附件回答（${pendingDocs.map((d) => d.filename).join("、")}）`
          : "";
    await submitValue(value, prefillMeta, trimmed.length === 0 && pendingDocs.length > 0);
  }

  async function dispatch(
    r: IntentResult,
    originalText: string,
    meta: CommandBarPrefillMeta | null,
  ): Promise<void> {
    switch (r.kind) {
      case "generate_html":
      case "generate_pptx":
      case "generate_xlsx":
      case "generate_word":
      case "generate_markdown":
      case "generate_pdf":
      case "generate_txt": {
        const kind = (r.params.artifact_type as string | undefined) ?? "html";
        const brief = (r.params.brief as string | undefined) ?? originalText;
        if (!brief) {
          message.warning("brief 为空，无法生成产物");
          return;
        }
        const now = new Date().toISOString();
        applyEvent({
          type: "rag.query",
          seq: 0,
          ts: now,
          payload: { question: originalText },
        });
        applyEvent({
          type: "chat.done",
          seq: 0,
          ts: new Date(Date.now() + 1).toISOString(),
          payload: {
            question: originalText,
            answer: `已开始${kindLabel[r.kind]}：${brief}\n\n我会先检索本地知识库和联网搜索结果，再基于检索材料生成，完成后会自动出现在右侧 outputs。`,
          },
        });
        message.info(`已派发：先检索知识库和联网搜索，再生成${kindLabel[r.kind]}（后台进行中）`);
        // 异步触发，结果通过 WS artifact.ready event 反馈到 store
        // 不 await，避免 busy/textarea 在 60-180s LLM 链路上一直锁住
        void generateArtifact({
          artifact_type: kind as
            | "html"
            | "pptx"
            | "xlsx"
            | "word"
            | "markdown"
            | "pdf"
            | "txt",
          brief,
          extra_instructions: [
            "生成前由后端统一执行本地知识库和联网证据 grounding；请严格基于自动检索证据生成。",
            "本地知识库优先；联网搜索只用于补充最新信息、市场信息或知识库缺口。",
            "不要生成泛化模板；每一页/每一节都要尽量落到检索材料里的具体事实。",
            "如果证据不足，请在产物中明确标注“资料不足/待补充”，不要凭空编造。",
            `用户原始需求：${originalText}`,
          ].join("\n"),
          meeting_id: meta?.meeting_id ?? currentMeetingId ?? undefined,
          todo_id: meta?.todo_id,
        })
          .then((art) => {
            addArtifact(art);
            const title = art.title || art.artifact_id;
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date().toISOString(),
              payload: {
                question: originalText,
                answer: `已生成 ${art.artifact_type.toUpperCase()}：${title}\n\n[下载/打开产物](${artifactDownloadUrl(art.artifact_id)})`,
              },
            });
            message.success(`已生成 ${art.artifact_type}`);
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date().toISOString(),
              payload: {
                question: originalText,
                answer: `生成 ${kindLabel[r.kind]} 失败：${msg}`,
              },
            });
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
      case "agent_task": {
        const taskText = (r.params.text as string | undefined) ?? originalText;
        if (!taskText) {
          message.warning("任务内容为空");
          return;
        }
        const task = await createAgentTask({
          text: taskText,
          title: (r.params.title as string | undefined) ?? taskText.slice(0, 42),
          task_kind: "agent_task",
          context: {
            current_meeting_id: currentMeetingId,
          },
        });
        upsertAgentTask(task);
        message.info(
          task.state === "waiting_permission" ? "需要授权后开始执行" : "已开始后台执行",
        );
        return;
      }
      case "chat_no_rag": {
        // P4-fix-rag-chat（2026-05-28）：显式 @chat → 纯 LLM 对话。
        // 注意：本 case 必须放在 default 之前，否则 fall-through 永远不会到。
        const question = (r.params.text as string | undefined) ?? originalText;
        if (!question) {
          message.warning("text 为空");
          return;
        }
        applyEvent({
          type: "rag.query",
          seq: 0,
          ts: new Date().toISOString(),
          payload: { question },
        });
        message.info("正在回复…");
        void chatAsk(question)
          .then((answer) => {
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date().toISOString(),
              payload: {
                question,
                answer,
              } as unknown as Record<string, unknown>,
            });
            message.success("已回复");
            void tts.speak(answer);
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            message.error(`回复失败：${msg}`);
          });
        return;
      }
      case "search_web":
      case "search_rag":
      case "chat":
      default: {
        // P4-fix-rag-chat（2026-05-28）：chat 分支默认也走 RAG。
        //
        // 用户痛点截图：上传 PDF 后输入"请基于附件回答（XX.pdf）"被分到 chat
        // → 旧代码只 toast 用户原文 + TTS 复述 → LLM 完全没调用、PDF 没用上。
        //
        // 新策略：chat / search_rag / search_web 都走 ragAsk（POST /rag/ask）。
        // backend retrieve_and_answer 会自己分类 rag / web / either，所有已索引
        // 的 docs（含 ambient + 上传 PDF + workspace 扫描结果）自动作为 context；
        // LLM 真的基于 PDF 答题 + TTS 朗读 LLM 输出（不是用户原文）。
        //
        // 显式 escape：r.kind="chat_no_rag"（@chat 前缀触发，见上面 case）→
        // 走 /chat 端点纯 LLM 闲聊，不查 RAG，避免"我就想说个'你好'还要 RAG"的浪费。
        const question = (r.params.question as string | undefined)
          ?? (r.params.text as string | undefined)
          ?? originalText;
        if (!question) {
          message.warning("question 为空");
          return;
        }
        applyEvent({
          type: "rag.query",
          seq: 0,
          ts: new Date().toISOString(),
          payload: { question },
        });
        message.info("已派发：检索中（后台进行中）");

        // 智能提示：当用户问问题但 RAG docs < 3 → 引导配置 workspace 目录
        // 让 RAG 覆盖整个文件夹，而不是只能用一两个手动上传的 PDF。
        // 不阻塞 ragAsk 主链路（并发触发）。
        void maybePromptWorkspaceConfig();

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
            const nCite = ans.citations?.length ?? 0;
            message.success(nCite > 0 ? `已回答（${nCite} 处引用）` : "已回答");
            // TTS 朗读 LLM 真实答案，而不是用户原文
            void tts.speak(ans.answer);
          })
          .catch((e) => {
            const msg = e instanceof Error ? e.message : String(e);
            message.error(`检索失败：${msg}`);
          });
        return;
      }
    }
  }

  return (
    <div
      className={`echodesk-command-bar relative border-t border-paper-300 bg-paper-100 px-4 py-2 transition ${
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
          松手即可加入知识库（PDF / Word / Excel / PPT / md / txt / html / csv …）
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

      {tvMode && (
        <div
          className="echodesk-tv-quick-commands flex flex-wrap items-center gap-2 mb-2"
          data-testid="tv-quick-commands"
        >
          {quickCommands.map((item) => (
            <button
              key={item.command}
              type="button"
              className="px-3 py-1.5 rounded-md border border-paper-300 bg-white text-[13px] text-ink-700 hover:border-accent hover:text-accent focus:outline-none focus:ring-2 focus:ring-accent/30"
              onClick={() => {
                if (busy || uploading > 0) return;
                setPrefillMeta(null);
                void submitValue(item.command, null, false);
              }}
              disabled={busy || uploading > 0}
              data-tv-clickable
            >
              {item.label}
            </button>
          ))}
        </div>
      )}

      <div className="echodesk-command-row flex items-center gap-2">
        <Wand2 className="echodesk-command-leading-icon w-4 h-4 text-ink-500 shrink-0" />
        <Input.TextArea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value.trim()) setPrefillMeta(null);
          }}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          onPaste={onPaste}
          placeholder={commandPlaceholder}
          rows={1}
          style={{
            minHeight: "44px",
            height: "44px",
            maxHeight: "44px",
          }}
          disabled={busy}
          className="echodesk-command-textarea !rounded-md"
          data-testid="command-textarea"
        />
        <Tooltip title="选择文件加入知识库（多选可批量）">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="echodesk-command-icon-btn shrink-0 p-1.5 rounded hover:bg-paper-200 text-ink-500 disabled:opacity-50"
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
        {/* P4-fix：显式 Send 按钮替代"只能按 Enter"的隐性 affordance；
            附件附了但文本空也允许点击（避免之前的 silent return）。 */}
        <Tooltip
          title={
            uploading > 0
              ? "入库中，请等待文件完成"
              : !canSubmit()
                ? "请输入文本或粘贴/拖入文件"
                : "发送（Enter）"
          }
        >
          <button
            type="button"
            onClick={() => void onSubmit()}
            className="echodesk-command-icon-btn shrink-0 p-1.5 rounded hover:bg-paper-200 text-accent disabled:opacity-40 disabled:text-ink-400 disabled:hover:bg-transparent"
            disabled={!canSubmit()}
            data-testid="command-send-btn"
            aria-label="发送"
          >
            {busy ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </button>
        </Tooltip>
      </div>
    </div>
  );
}

// 占位防止未来扩展时 lint 警告（X 用于 chip 关闭按钮的扩展位）
void X;

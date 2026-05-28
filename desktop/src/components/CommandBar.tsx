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

import { useCallback, useEffect, useRef, useState } from "react";
import { Input, Tag, Tooltip, message } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { FileText, Loader2, Paperclip, Send, Upload, Wand2, X } from "lucide-react";

import {
  chatAsk,
  finalizeMeeting,
  generateArtifactStream,
  ingestFile,
  listRagDocs,
  listRecentAmbient,
  ragAsk,
  routeIntent,
  workspaceStatus,
} from "@/api";
import { useStore } from "@/store";
import type { IntentKind, IntentResult } from "@/types";
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
  chat_no_rag: "纯闲聊",
  chat: "对话",
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
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const addArtifact = useStore((s) => s.addArtifact);
  const applyEvent = useStore((s) => s.applyEvent);
  const registerCommandBarPrefill = useStore((s) => s.registerCommandBarPrefill);
  const appendUserCommand = useStore((s) => s.appendUserCommand);
  const appendAssistantReply = useStore((s) => s.appendAssistantReply);
  const patchConversationStatus = useStore((s) => s.patchConversationStatus);
  const patchAssistantReply = useStore((s) => s.patchAssistantReply);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<TextAreaRef | null>(null);

  // M_minutes_refactor：MinutesView「执行待办」按钮通过 store.prefillCommandBar
  // 把 suggested_command 一键填入；只 setText + focus，不自动 onSubmit 防误触。
  useEffect(() => {
    const unregister = registerCommandBarPrefill((nextText) => {
      setText(nextText);
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
            "📂 想覆盖整个文件夹？点 设置（齿轮） → 工作区目录，加一个 ~/Documents 之类的目录，RAG 会自动扫描索引",
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

  /**
   * 拼接喂给 echo 的 inline_context：当前会议 segments + 最近 ambient（最多 30 条）。
   *
   * 用户 2026-05-28：「Echo 的回复显然没有带上下文和知识库或者网络搜索」——
   * RAG 索引只覆盖结束的会议 / 上传的 PDF，进行中的转录还没进索引。这里前端
   * 把最近 30 条转录拼成可读字符串作为 inline_context 透传给 retrieve_and_answer，
   * 让 Echo 答题时能感知"我们刚才在聊什么"。
   */
  async function buildInlineContext(): Promise<string> {
    try {
      const recent = await listRecentAmbient(30);
      if (recent.length === 0) return "";
      const lines = recent.map((s) => {
        const tag = s.speaker_label ?? s.speaker_id ?? "?";
        return `${tag} · ${s.text}`;
      });
      return lines.join("\n");
    } catch {
      return "";
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
    if (!value) return;
    // 用户 2026-05-28 反馈：「我输入的命令也要进转写流（右边）」+ 「这里 @ 应
    // 该是 @echo，不是空着」——立刻 append 一条 user_command 事件，文本前补
    // `@echo`（如果用户没主动打前缀），让气泡明确显示"在问 echo"。
    // TranscriptStream 渲染为右侧气泡。拿到的 cmdId 在 dispatch 结束时 patch
    // status="done"/"failed"。
    const displayValue = value.startsWith("@") ? value : `@echo ${value}`;
    const cmdId = appendUserCommand(displayValue);
    // P4-fix（2026-05-28）：提交瞬间在左侧预插一个 status="pending" 的空 Echo 气泡，
    // dispatch 各分支拿到回答后 patchAssistantReply(replyId, {...})。
    // 这样 TranscriptStream 的"Echo 思考中…"spinner 落在 Echo 回复气泡（左），
    // 而不是用户命令气泡（右）。
    const replyId = appendAssistantReply(
      "",
      "assistant_reply",
      undefined,
      "pending",
    );
    setBusy(true);
    try {
      const r = await routeIntent(value, currentMeetingId);
      await dispatch(r, value, cmdId, replyId);
      setText("");
      if (trimmed.length === 0 && pendingDocs.length > 0) {
        setPendingDocs([]);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      patchConversationStatus(cmdId, "failed");
      patchAssistantReply(replyId, {
        text: `发送失败：${msg}`,
        status: "failed",
      });
    } finally {
      setBusy(false);
    }
  }

  async function dispatch(
    r: IntentResult,
    originalText: string,
    cmdId: string,
    replyId: string,
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
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: "brief 为空，无法生成产物",
            status: "failed",
          });
          return;
        }
        // 让 pending Echo 气泡继续转 spinner，同时把"已派发"文案 patch 进 text，
        // 用户能看到"任务被识别 → 正在生成"两层进度。
        patchAssistantReply(replyId, {
          text: `已派发：${kindLabel[r.kind]}，准备 prompt 中…`,
        });
        // 用户原话："不管调用什么工具或者 skill，最好也能流式输出一些过程性的内容"。
        // 走 SSE 版 /artifacts/generate/stream：把每个阶段（phase / llm_chunk /
        // done / error）实时 patch 到 Echo 气泡，让用户看见生成全过程而不是 5
        // 分钟 spinner。stream 函数本身只会因 fetch 失败抛错；业务错误走 onError。
        let finalLabel = kindLabel[r.kind];
        void generateArtifactStream(
          {
            artifact_type: kind as
              | "html"
              | "pptx"
              | "xlsx"
              | "word"
              | "markdown"
              | "pdf"
              | "txt",
            brief,
          },
          {
            onPhase: (p) => {
              // phase 类事件直接把 msg 当文案；缺 msg 时回退到 phase 名。
              const human = p.msg ?? p.phase;
              patchAssistantReply(replyId, {
                text: `${kindLabel[r.kind]} · ${human}`,
                status: "pending",
              });
            },
            onLLMChunk: ({ text, total_chars }) => {
              // 只显示尾部 ~300 字预览，避免气泡膨胀到几千字（HTML one-pager
              // 6000+ chars，PPT JSON 27 字段 + 解释也常 2000+ chars）。
              const tail = text.length > 300 ? text.slice(-300) : text;
              patchAssistantReply(replyId, {
                text: `${kindLabel[r.kind]} · 生成中（已收到 ${total_chars} 字符）…\n\n${tail}`,
                status: "pending",
              });
            },
            onDone: (art) => {
              addArtifact(art);
              finalLabel = art.artifact_type;
              patchConversationStatus(cmdId, "done");
              const sizeKb = (art.size_bytes / 1024).toFixed(1);
              const title = art.title?.trim() ? art.title : art.artifact_id;
              patchAssistantReply(replyId, {
                text: `已生成 ${finalLabel} · ${title}（${sizeKb} KB）`,
                status: "done",
              });
            },
            onError: ({ error, stage }) => {
              patchConversationStatus(cmdId, "failed");
              const where = stage ? `（阶段 ${stage}）` : "";
              patchAssistantReply(replyId, {
                text: `生成失败${where}：${error}`,
                status: "failed",
              });
            },
          },
        ).catch((e) => {
          const raw = e instanceof Error ? e.message : String(e);
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: `生成失败（连接异常）：${raw}`,
            status: "failed",
          });
        });
        return;
      }
      case "summarize_meeting": {
        const mid = (r.params.meeting_id as string | undefined) ?? currentMeetingId;
        if (!mid) {
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: "当前没有进行中的会议",
            status: "failed",
          });
          return;
        }
        patchAssistantReply(replyId, { text: `正在总结会议 ${mid}…` });
        try {
          const minutes = await finalizeMeeting(mid, `会议 ${mid}`);
          patchConversationStatus(cmdId, "done");
          patchAssistantReply(replyId, {
            text: `会议纪要已生成，共 ${minutes.sections.length} 节`,
            status: "done",
          });
        } catch (e) {
          const raw = e instanceof Error ? e.message : String(e);
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: `总结失败：${raw}`,
            status: "failed",
          });
        }
        return;
      }
      case "chat_no_rag": {
        const question = (r.params.text as string | undefined) ?? originalText;
        if (!question) {
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: "text 为空",
            status: "failed",
          });
          return;
        }
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
            patchConversationStatus(cmdId, "done");
            patchAssistantReply(replyId, {
              text: answer,
              status: "done",
            });
            // 用户 2026-05-28 反馈：自动播放且关不掉。默认不再 auto-speak，
            // 用户想听就点 StatusBar 的 TTS 按钮，或后续在 Echo 气泡上点 🔊。
          })
          .catch((e) => {
            const raw = e instanceof Error ? e.message : String(e);
            patchConversationStatus(cmdId, "failed");
            patchAssistantReply(replyId, {
              text: `闲聊失败：${raw}`,
              status: "failed",
            });
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
          patchConversationStatus(cmdId, "failed");
          patchAssistantReply(replyId, {
            text: "question 为空",
            status: "failed",
          });
          return;
        }
        // 智能提示：当用户问问题但 RAG docs < 3 → 引导配置 workspace 目录
        // 不阻塞 ragAsk 主链路（并发触发）。
        void maybePromptWorkspaceConfig();

        // 拼最近 ambient 转录作为 inline_context 喂给 Echo（用户 2026-05-28 反馈
        // "回复没带上下文"）。buildInlineContext 失败返回 ""，不阻塞主链路
        const inlineContext = await buildInlineContext();

        void ragAsk(question, { inlineContext })
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
            patchConversationStatus(cmdId, "done");
            // 引用映射到 ConversationEvent 的 citations 字段
            const cites = (ans.citations ?? [])
              .filter((c): c is typeof c & { doc_id: string } =>
                typeof c.doc_id === "string" && c.doc_id.length > 0,
              )
              .map((c) => ({
                doc_id: c.doc_id,
                chunk_id: (c as unknown as { chunk_id?: string }).chunk_id,
                score: c.score,
              }));
            patchAssistantReply(replyId, {
              text: ans.answer,
              kind: "rag_answer",
              citations: cites,
              status: "done",
            });
            // 用户 2026-05-28：默认不 auto-speak（见上文 chat 分支注释）。
          })
          .catch((e) => {
            const raw = e instanceof Error ? e.message : String(e);
            patchConversationStatus(cmdId, "failed");
            patchAssistantReply(replyId, {
              text: `检索失败：${raw}`,
              status: "failed",
            });
          });
        return;
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

      {/* 用户 2026-05-28：「置信度/关键字命中 这些后台信息不要显示」——
          移除 intent-status 行；意图标签 / rationale / confidence 现在仅
          作为内部状态用于 dispatch，不渲染到 UI。 */}

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
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          onPaste={onPaste}
          placeholder="问 Echo（默认带知识库 + 网络 + 当前会议上下文）· @生成 PPT / @生成 HTML / @chat 闲聊 · 拖入文件入库 RAG · Shift+Enter 换行"
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
            className="shrink-0 p-1.5 rounded hover:bg-paper-200 text-accent disabled:opacity-40 disabled:text-ink-400 disabled:hover:bg-transparent"
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

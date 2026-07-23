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

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Tag, Tooltip, message } from "antd";
import { ArrowUp, FileText, Loader2, Paperclip, Upload } from "lucide-react";

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
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { useTtsPlayer } from "@/hooks/useTtsPlayer";
import { meetingDisplayTitle } from "@/lib/meetingDisplay";
import { resolveIntentPlanGate } from "@/lib/intentPlanGate";
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
  const currentMeeting = useStore((s) =>
    s.currentMeetingId ? s.meetings[s.currentMeetingId] : undefined,
  );
  const applyEvent = useStore((s) => s.applyEvent);
  const upsertAgentTask = useStore((s) => s.upsertAgentTask);
  const registerCommandBarPrefill = useStore((s) => s.registerCommandBarPrefill);
  const tts = useTtsPlayer();
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
  } = useBackendOriginFence();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [tvMode, setTvMode] = useState(() => detectTvCommandMode());
  const commandPlaceholder = tvMode
    ? "询问当前记录，或描述要完成的任务"
    : "询问这段记录，或描述要生成的内容";
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
      const textarea = textareaRef.current;
      if (textarea) {
        textarea.focus();
        textarea.setSelectionRange(nextText.length, nextText.length);
      }
    });
    return unregister;
  }, [registerCommandBarPrefill]);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    const styles = window.getComputedStyle(textarea);
    const minHeight = Number.parseFloat(styles.minHeight) || 0;
    const parsedMaxHeight = Number.parseFloat(styles.maxHeight);
    const maxHeight = Number.isFinite(parsedMaxHeight)
      ? parsedMaxHeight
      : Number.POSITIVE_INFINITY;
    const nextHeight = Math.max(
      minHeight,
      Math.min(textarea.scrollHeight, maxHeight),
    );
    textarea.style.height = `${nextHeight}px`;
  }, [text, tvMode]);

  // P4-fix-rag-chat 智能提示节流：每条会话最多提示一次，避免每次发问都弹。
  const workspaceHintShownRef = useRef(false);

  useEffect(() => {
    // doc_id、待办 prefill 与 busy/uploading 都属于创建它们的 backend origin。
    // origin 切换后立即清空，旧异步链路再晚返回也只能被 generation fence 丢弃。
    setText("");
    setPrefillMeta(null);
    setPendingDocs([]);
    setBusy(false);
    setUploading(0);
    setDropActive(false);
    workspaceHintShownRef.current = false;
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [backendOriginRevision]);

  // 当用户走 search_rag 但 RAG docs 数太少（< 3）且没配置 workspace_dirs 时，
  // 当资料过少时提示用户添加文件夹，避免依赖隐藏命令记忆。
  const maybePromptWorkspaceConfig = useCallback(async (
    originGeneration: number,
  ): Promise<void> => {
    if (!isCurrent(originGeneration) || workspaceHintShownRef.current) return;
    try {
      const [docs, ws] = await Promise.all([
        listRagDocs(),
        workspaceStatus(),
      ]);
      if (!isCurrent(originGeneration)) return;
      const nDocs = docs.total ?? 0;
      const nConfigured = ws.configured_dirs?.length ?? 0;
      if (nDocs < 3 && nConfigured === 0) {
        workspaceHintShownRef.current = true;
        message.info({
          content:
            "资料较少。可在“知识库设置”中添加文件夹，EchoDesk 会自动建立索引。",
          duration: 8,
        });
      }
    } catch {
      /* 静默：智能提示不应阻塞用户主问答 */
    }
  }, [isCurrent]);

  const handleFiles = useCallback(async (files: FileList | File[]): Promise<void> => {
    const arr = Array.from(files);
    if (!arr.length) return;
    const originGeneration = captureGeneration();
    for (const f of arr) {
      if (!isCurrent(originGeneration)) return;
      const ext = pickExt(f.name);
      if (!ACCEPT_EXT_SET.has(ext)) {
        message.warning(`不支持的文件类型：${f.name}（${ext || "无后缀"}）`);
        continue;
      }
      setUploading((n) => n + 1);
      try {
        const r = await ingestFile(f, f.name);
        if (!isCurrent(originGeneration)) return;
        setPendingDocs((prev) => [
          ...prev,
          { doc_id: r.doc_id, title: r.title, filename: f.name },
        ]);
        message.success(`已添加到知识库：${f.name}`);
      } catch (e) {
        if (!isCurrent(originGeneration)) return;
        console.error("[command-bar] failed to add file to knowledge base", e);
        message.error(`${f.name} 添加失败，请检查文件后重试`);
      } finally {
        if (isCurrent(originGeneration)) {
          setUploading((n) => Math.max(0, n - 1));
        }
      }
    }
  }, [captureGeneration, isCurrent]);

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
    const originGeneration = captureGeneration();
    if (!isCurrent(originGeneration)) return;
    const interactionMeetingId = activePrefillMeta?.meeting_id ?? currentMeetingId ?? undefined;
    const submittedAt = Date.now();
    const messageId = `message_${submittedAt}_${Math.random().toString(36).slice(2, 9)}`;
    // 用户消息必须先于意图路由和任何模型调用进入对话流，避免路由延迟造成“发送无响应”。
    applyEvent({
      type: "rag.query",
      seq: 0,
      ts: new Date(submittedAt).toISOString(),
      meeting_id: interactionMeetingId,
      payload: { question: value, message_id: messageId },
    });
    setBusy(true);
    try {
      const availableContext = [
        currentMeeting
          ? `当前会话：${meetingDisplayTitle(currentMeeting, "未命名会话")}`
          : null,
        ...pendingDocs.map((doc) => `可用资料：${doc.filename}`),
        activePrefillMeta?.todo_id ? `当前待办：${activePrefillMeta.todo_id}` : null,
      ].filter((item): item is string => Boolean(item));
      // @ 命令和普通输入统一由后端 V4 Flash 结构化计划；前端不再选择
      // artifact 模板或构造任何 direct-dispatch IntentResult。
      const r = await routeIntent(value, currentMeetingId, availableContext);
      if (!isCurrent(originGeneration)) return;
      await dispatch(r, value, activePrefillMeta, originGeneration, messageId, availableContext);
      if (!isCurrent(originGeneration)) return;
      setText("");
      setPrefillMeta(null);
      // 附件已被发出，清空 pendingDocs（后端 RAG 检索复用 doc_id）
      if (clearPendingAfterSubmit) {
        setPendingDocs([]);
      }
    } catch (e) {
      if (!isCurrent(originGeneration)) return;
      console.error("[command-bar] submit failed", e);
      message.error("发送失败，请稍后重试");
    } finally {
      if (isCurrent(originGeneration)) setBusy(false);
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
    originGeneration: number,
    messageId: string,
    availableContext: string[],
  ): Promise<void> {
    if (!isCurrent(originGeneration)) return;
    const interactionMeetingId = meta?.meeting_id ?? currentMeetingId ?? undefined;
    const conversationId = interactionMeetingId
      ? `meeting:${interactionMeetingId}`
      : "global";
    const intentGate = resolveIntentPlanGate(r);
    if (!intentGate.allowDispatch) {
      applyEvent({
        type: "chat.done",
        seq: 0,
        ts: new Date().toISOString(),
        meeting_id: interactionMeetingId,
        payload: { question: originalText, answer: intentGate.message },
      });
      if (intentGate.failed) message.error("需求规划失败，请稍后重试");
      else message.info("请补充信息后再执行");
      return;
    }
    const emitMemoryFrame = (frame: import("@/types").MemoryFramePayload): void => {
      if (!isCurrent(originGeneration)) return;
      applyEvent({
        type: frame.type,
        seq: 0,
        ts: new Date().toISOString(),
        meeting_id: interactionMeetingId,
        payload: frame as unknown as Record<string, unknown>,
      });
    };
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
          message.warning("请先描述想生成的内容");
          return;
        }
        applyEvent({
          type: "chat.done",
          seq: 0,
          ts: new Date(Date.now() + 1).toISOString(),
          meeting_id: interactionMeetingId,
          payload: {
            question: originalText,
            answer: `已开始${kindLabel[r.kind]}：${brief}\n\n我会先检索本地知识库和联网资料，再基于证据生成。完成后可在右侧“工作产物”中查看。`,
          },
        });
        message.info(`正在整理资料并${kindLabel[r.kind]}`);
        // 异步触发，结果通过 WS artifact.ready event 反馈到 store
        // 不 await，避免 busy/textarea 在 60-180s LLM 链路上一直锁住
        const intentPlan = intentGate.serializedPlan;
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
            ...(intentPlan ? [`已验证的 intent plan：${intentPlan}`] : []),
            `用户原始需求：${originalText}`,
          ].join("\n"),
          meeting_id: meta?.meeting_id ?? currentMeetingId ?? undefined,
          todo_id: meta?.todo_id,
          retry_of_run_id: meta?.retry_of_run_id,
          context_refs: intentGate.contextRefs,
        })
          .then((art) => {
            if (!isCurrent(originGeneration)) return;
            const title = art.title?.trim() || `未命名${kindLabel[r.kind].replace("生成 ", "")}`;
            const completedAt = Date.now();
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date(completedAt).toISOString(),
              meeting_id: interactionMeetingId,
              payload: {
                question: originalText,
                answer: `已生成 ${art.artifact_type.toUpperCase()}：${title}\n\n[下载/打开产物](${artifactDownloadUrl(art.artifact_id)})`,
              },
            });
            applyEvent({
              type: "artifact.ready",
              seq: 0,
              ts: new Date(completedAt + 1).toISOString(),
              meeting_id: interactionMeetingId,
              payload: art as unknown as Record<string, unknown>,
            });
            message.success(`已生成 ${art.artifact_type}`);
          })
          .catch((e) => {
            if (!isCurrent(originGeneration)) return;
            console.error("[command-bar] artifact generation failed", e);
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date().toISOString(),
              meeting_id: interactionMeetingId,
              payload: {
                question: originalText,
                answer: `${kindLabel[r.kind]}失败。请检查要求后重试；已有文件不会受影响。`,
              },
            });
            message.error("生成失败，请稍后重试");
          });
        return;
      }
      case "summarize_meeting": {
        const mid = (r.params.meeting_id as string | undefined) ?? currentMeetingId;
        if (!mid) {
          message.warning("当前没有进行中的会议");
          return;
        }
        message.info("正在生成会议纪要…");
        void finalizeMeeting(
          mid,
          meetingDisplayTitle(currentMeeting, "会议纪要"),
        )
          .then((minutes) => {
            if (!isCurrent(originGeneration)) return;
            message.success(`会议纪要已生成，共 ${minutes.sections.length} 节`);
          })
          .catch((e) => {
            if (!isCurrent(originGeneration)) return;
            console.error("[command-bar] meeting finalization failed", e);
            message.error("会议纪要生成失败，请稍后重试");
          });
        return;
      }
      case "agent_task": {
        const taskText = (r.params.text as string | undefined) ?? originalText;
        if (!taskText) {
          message.warning("请先描述需要完成的任务");
          return;
        }
        const submittedAt = Date.now();
        const task = await createAgentTask({
          text: taskText,
          title: (r.params.title as string | undefined) ?? taskText.slice(0, 42),
          task_kind: "agent_task",
          conversation_id: conversationId,
          message_id: messageId,
          available_context: availableContext,
          context: {
            meeting_id: interactionMeetingId,
          },
        });
        if (!isCurrent(originGeneration)) return;
        upsertAgentTask(task);
        applyEvent({
          type: "chat.done",
          seq: 0,
          ts: new Date(submittedAt + 1).toISOString(),
          meeting_id: interactionMeetingId,
          payload: {
            question: taskText,
            answer:
              task.state === "waiting_permission"
                ? "已记录请求。需要授权后我会开始后台执行。"
                : "已记录请求，并提交到后台执行。完成后会出现在右侧“工作产物”。",
          },
        });
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
          message.warning("请输入问题");
          return;
        }
        message.info("正在回复…");
        void chatAsk(question, {
          conversationId,
          messageId,
          onMemoryFrame: emitMemoryFrame,
        })
          .then((answer) => {
            if (!isCurrent(originGeneration)) return;
            applyEvent({
              type: "chat.done",
              seq: 0,
              ts: new Date().toISOString(),
              meeting_id: interactionMeetingId,
              payload: {
                question,
                answer,
                message_id: messageId,
              } as unknown as Record<string, unknown>,
            });
            message.success("已回复");
            void tts.speak(answer);
          })
          .catch((e) => {
            if (!isCurrent(originGeneration)) return;
            console.error("[command-bar] direct chat failed", e);
            message.error("暂时无法回复，请稍后重试");
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
          message.warning("请输入问题");
          return;
        }
        message.info("正在检索相关资料…");

        // 智能提示：当用户问问题但 RAG docs < 3 → 引导配置 workspace 目录
        // 让 RAG 覆盖整个文件夹，而不是只能用一两个手动上传的 PDF。
        // 不阻塞 ragAsk 主链路（并发触发）。
        void maybePromptWorkspaceConfig(originGeneration);

        void ragAsk(question, {
          conversationId,
          messageId,
          onMemoryFrame: emitMemoryFrame,
        })
          .then((ans) => {
            if (!isCurrent(originGeneration)) return;
            applyEvent({
              type: "rag.answer.done",
              seq: 0,
              ts: new Date().toISOString(),
              meeting_id: interactionMeetingId,
              payload: {
                question,
                answer: ans.answer,
                citations: ans.citations,
                arbitration: ans.arbitration,
                message_id: messageId,
              } as unknown as Record<string, unknown>,
            });
            const nCite = ans.citations?.length ?? 0;
            message.success(nCite > 0 ? `已回答（${nCite} 处引用）` : "已回答");
            // TTS 朗读 LLM 真实答案，而不是用户原文
            void tts.speak(ans.answer);
          })
          .catch((e) => {
            if (!isCurrent(originGeneration)) return;
            console.error("[command-bar] retrieval failed", e);
            message.error("检索失败，请稍后重试");
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
              title={d.filename}
            >
              {d.filename}
            </Tag>
          ))}
          {uploading > 0 && (
            <span className="inline-flex items-center gap-1 text-[11px] text-ink-500">
              <Loader2 className="w-3 h-3 animate-spin" />
              正在添加 {uploading} 个文件…
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
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value.trim()) setPrefillMeta(null);
          }}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          onPaste={onPaste}
          placeholder={commandPlaceholder}
          rows={1}
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
              <ArrowUp className="w-4 h-4" />
            )}
          </button>
        </Tooltip>
      </div>
    </div>
  );
}

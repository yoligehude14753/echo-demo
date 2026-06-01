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
  runAgent,
  routeIntent,
  workspaceStatus,
  type ArtifactKind,
} from "@/api";
import { type CommandBarPrefillMeta, useStore } from "@/store";
import type { GeneratedArtifact, IntentResult } from "@/types";
import { extractExplicitArtifactCommand } from "@/lib/explicitArtifactCommand";
import type { TtsController } from "@/hooks/useTtsPlayer";
import { toSpeakableAnswer } from "@/lib/voiceWake";
import { createTtsSentenceStreamer } from "@/lib/ttsStream";
interface PendingDoc {
  doc_id: string;
  title: string;
  filename: string;
}

const agentToolLabel: Record<string, string> = {
  rag_search: "查知识库",
  web_search: "联网搜索",
  generate_artifact: "生成产物",
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

export default function CommandBar({ tts }: { tts?: TtsController }): JSX.Element {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(0);
  const [dropActive, setDropActive] = useState(false);
  const [pendingDocs, setPendingDocs] = useState<PendingDoc[]>([]);
  const [prefillMeta, setPrefillMeta] = useState<CommandBarPrefillMeta | null>(null);
  const currentMeetingId = useStore((s) => s.currentMeetingId);
  const addArtifact = useStore((s) => s.addArtifact);
  const beginRun = useStore((s) => s.beginRun);
  const applyEvent = useStore((s) => s.applyEvent);
  const upsertMeeting = useStore((s) => s.upsertMeeting);
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
    const submitMeta = prefillMeta;
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
      const explicitArtifact = extractExplicitArtifactCommand(value);
      if (explicitArtifact) {
        await runArtifactGenerationWorkflow(
          explicitArtifact.artifactType,
          explicitArtifact.brief,
          submitMeta,
          cmdId,
          replyId,
        );
      } else {
        const r = await routeIntent(value, submitMeta?.meeting_id ?? currentMeetingId);
        await dispatch(r, value, cmdId, replyId, submitMeta);
      }
      setText("");
      setPrefillMeta(null);
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

  async function runAgentWorkflow(
    question: string,
    cmdId: string,
    replyId: string,
  ): Promise<void> {
    const progressLines: string[] = [];
    let finalAnswer = "";
    let sawFinal = false;
    let sawError = false;
    let sawToolCall = false;
    let artifacts: GeneratedArtifact[] = [];

    const renderPending = (body = finalAnswer): void => {
      patchAssistantReply(replyId, {
        text: [progressLines.slice(-6).join("\n"), body].filter(Boolean).join("\n\n"),
        status: "pending",
      });
    };

    // 句级流式 TTS：边出文本边逐句播，避免播放比文本晚太多
    const ttsStreamer = tts ? createTtsSentenceStreamer(tts) : null;
    // 可中止：注册到全局运行控制，「停止」按钮会 abort + 停 TTS
    const controller = new AbortController();
    const endRun = beginRun(() => {
      controller.abort();
      tts?.cancel();
    });
    try {
      // 初始只显示中性提示，不提"多工具"——简单问答根本不会调工具
      renderPending("Echo 正在思考…");
      const inlineContext = await buildInlineContext();
      await runAgent(
        question,
        { inlineContext, maxIterations: 6, signal: controller.signal },
        {
          // onPlan 不再更新 UI，避免一上来就显示 1/6 步数
          onPlan: () => {},
          onToolCall: ({ name, reason }) => {
            // 第一次真正调工具时才切换到"多工具"提示
            if (!sawToolCall) {
              sawToolCall = true;
              renderPending("Echo 正在调用工具…");
            }
            const label = agentToolLabel[name] ?? name;
            progressLines.push(`→ ${label}${reason ? `：${reason}` : ""}`);
            renderPending();
          },
          onToolResult: ({ name, ok, summary }) => {
            const label = agentToolLabel[name] ?? name;
            progressLines.push(`${ok ? "✓" : "!"} ${label}${summary ? `：${summary}` : ""}`);
            renderPending();
          },
          onArtifact: (artifact) => {
            addArtifact(artifact);
            artifacts = [
              artifact,
              ...artifacts.filter((a) => a.artifact_id !== artifact.artifact_id),
            ];
            progressLines.push(
              `✓ 已生成 ${artifact.artifact_type}：${artifact.title || artifact.artifact_id}`,
            );
            patchAssistantReply(replyId, { artifacts });
            renderPending();
          },
          onDelta: (chunk) => {
            finalAnswer += chunk;
            renderPending(finalAnswer);
            ttsStreamer?.push(chunk);
          },
          onFinal: ({ answer, citations }) => {
            sawFinal = true;
            finalAnswer = answer;
            const cites = (citations ?? [])
              .filter((c) => c.kind === "rag" || c.kind === "web")
              .slice(0, 20);
            patchConversationStatus(cmdId, "done");
            patchAssistantReply(replyId, {
              text: answer,
              kind: cites.length > 0 ? "rag_answer" : "assistant_reply",
              citations: cites,
              artifacts,
              status: "done",
            });
            ttsStreamer?.finalize(toSpeakableAnswer(answer));
          },
          onError: ({ error, stage }) => {
            sawError = true;
            patchConversationStatus(cmdId, "failed");
            patchAssistantReply(replyId, {
              text: `多工具执行失败${stage ? `（${stage}）` : ""}：${error}`,
              artifacts,
              status: "failed",
            });
          },
        },
      );
      if (!sawFinal && !sawError) {
        patchConversationStatus(cmdId, "done");
        patchAssistantReply(replyId, {
          text: finalAnswer || progressLines.join("\n") || "任务已结束。",
          artifacts,
          status: "done",
        });
      }
    } catch (e) {
      if (controller.signal.aborted) {
        patchConversationStatus(cmdId, "done");
        patchAssistantReply(replyId, {
          text: finalAnswer || "已停止。",
          artifacts,
          status: "done",
        });
      } else {
        const raw = e instanceof Error ? e.message : String(e);
        patchConversationStatus(cmdId, "failed");
        patchAssistantReply(replyId, {
          text: `多工具执行失败（连接异常）：${raw}`,
          artifacts,
          status: "failed",
        });
      }
    } finally {
      endRun();
    }
  }

  async function runArtifactGenerationWorkflow(
    artifactType: ArtifactKind,
    brief: string,
    meta: CommandBarPrefillMeta | null,
    cmdId: string,
    replyId: string,
  ): Promise<void> {
    let sawDone = false;
    let sawError = false;
    let lastProgress = "正在生成产物…";
    const controller = new AbortController();
    const endRun = beginRun(() => controller.abort());
    try {
      patchAssistantReply(replyId, {
        text: "正在执行会议待办并生成产物…",
        status: "pending",
      });
      // meeting_id 优先取 todo prefill 的，没有则用当前选中的会议
      // （伴随时段 currentMeetingId === null，此时不传，产物归全局）
      const effectiveMeetingId = meta?.meeting_id ?? currentMeetingId ?? undefined;
      await generateArtifactStream(
        {
          artifact_type: artifactType,
          brief,
          meeting_id: effectiveMeetingId,
          todo_id: meta?.todo_id,
        },
        {
          onPhase: ({ msg, phase, total_chars }) => {
            lastProgress = msg || phase || lastProgress;
            const suffix =
              typeof total_chars === "number" && total_chars > 0
                ? `\n已收到 ${total_chars} 字符`
                : "";
            patchAssistantReply(replyId, {
              text: `${lastProgress}${suffix}`,
              status: "pending",
            });
          },
          onLLMChunk: ({ text: chunk }) => {
            patchAssistantReply(replyId, {
              text: chunk.slice(-600) || lastProgress,
              status: "pending",
            });
          },
          onDone: (artifact) => {
            sawDone = true;
            addArtifact(artifact);
            patchConversationStatus(cmdId, "done");
            patchAssistantReply(replyId, {
              text: `已生成 ${artifact.artifact_type}：${artifact.title || artifact.artifact_id}`,
              artifacts: [artifact],
              status: "done",
            });
          },
          onError: ({ error, stage }) => {
            sawError = true;
            patchConversationStatus(cmdId, "failed");
            patchAssistantReply(replyId, {
              text: `生成失败${stage ? `（${stage}）` : ""}：${error}`,
              status: "failed",
            });
          },
        },
        controller.signal,
      );
      if (!sawDone && !sawError) {
        patchConversationStatus(cmdId, "failed");
        patchAssistantReply(replyId, {
          text: "生成失败：后端流结束但没有返回产物",
          status: "failed",
        });
      }
    } catch (e) {
      if (controller.signal.aborted) {
        patchConversationStatus(cmdId, "done");
        patchAssistantReply(replyId, { text: "已停止。", status: "done" });
      } else {
        const raw = e instanceof Error ? e.message : String(e);
        patchConversationStatus(cmdId, "failed");
        patchAssistantReply(replyId, {
          text: `生成失败（连接异常）：${raw}`,
          status: "failed",
        });
      }
    } finally {
      endRun();
    }
  }

  async function dispatch(
    r: IntentResult,
    originalText: string,
    cmdId: string,
    replyId: string,
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
        const artifactType = r.kind.replace("generate_", "") as ArtifactKind;
        const brief =
          (r.params.brief as string | undefined) ??
          (r.params.text as string | undefined) ??
          originalText;
        await runArtifactGenerationWorkflow(
          artifactType,
          brief,
          meta,
          cmdId,
          replyId,
        );
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
        upsertMeeting(mid, {
          state: "ended",
          minutes_status: "generating",
        });
        try {
          const minutes = await finalizeMeeting(mid, `会议 ${mid}`);
          upsertMeeting(mid, {
            minutes,
            title: minutes.title,
            display_title: minutes.title,
            state: "ended",
            minutes_status: "ok",
            minutes_error: null,
          });
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
        // 新策略：chat / search_rag / search_web 都走 agent（POST /agent/run）。
        // backend agent 会自行串联 rag_search / web_search / generate_artifact；
        // 所有已索引 docs（含 ambient + 上传 PDF + workspace 扫描结果）可作为 context。
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
        // 不阻塞 agent 主链路（并发触发）。
        void maybePromptWorkspaceConfig();
        await runAgentWorkflow(question, cmdId, replyId);
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
          松手即可加入知识库（PDF / Word / Excel / PPT / md / txt / html / csv …）
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
          onChange={(e) => {
            setText(e.target.value);
            setPrefillMeta(null);
          }}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          onPaste={onPaste}
          placeholder="问 Echo（默认带知识库、网络和当前会议上下文）· @生成 PPT / @生成 HTML / @chat 闲聊 · 拖入文件加入知识库 · Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={busy}
          className="!rounded-md"
          data-testid="command-textarea"
        />
        <Tooltip title="选择文件加入知识库（多选可批量）">
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

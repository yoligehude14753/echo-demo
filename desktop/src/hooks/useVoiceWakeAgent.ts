import { message } from "antd";
import { useCallback, useRef } from "react";
import {
  generateArtifactStream,
  getDailyRecap,
  listRecentAmbient,
  runAgent,
} from "@/api";
import type { TtsController } from "@/hooks/useTtsPlayer";
import { extractExplicitArtifactCommand } from "@/lib/explicitArtifactCommand";
// 唤醒词匹配 + 朗读文本归一统一走 @/lib/voiceWake（单一真源）。
// 早期此处内联过一份小词表，已合并到 lib 并扩充鲁棒性；这里 re-export 保持兼容。
import {
  containsWakeWord,
  extractEchoWakeCommand,
  isDailyRecapCommand,
  isLikelyEchoFollowup,
  toSpeakableAnswer,
} from "@/lib/voiceWake";
import { createTtsSentenceStreamer } from "@/lib/ttsStream";
import { useStore } from "@/store";

export { extractEchoWakeCommand, toSpeakableAnswer };

const DUPLICATE_WINDOW_MS = 12_000;
// 指令累积 endpointing（对齐老 echo 的 VAD endpointing）：检测到唤醒后不立刻
// 执行，把后续 chunk 累积进缓冲，直到检测到**真实静音**（某个 chunk 没产出
// 文本=用户停顿）才执行——避免"用户没说完 Echo 就抢答"。
//
// 关键教训：不能用"句子以。！？结尾"当说完信号——STT 标点后处理给**每个**
// chunk 末尾都加句号，导致每段都被误判成说完，在下一段到来前就抢跑。
//
// 采集层已做 VAD 断句（每段都是「完整一句」），所以这里只需在「收到一句后再
// 等一小段、看用户是否继续说下一句」。VAD 段之间的间隔就是用户的自然停顿，
// 因此防抖窗口可以比固定分块时代短很多。
// 自适应断点：真正的"说完"由采集层的静音检测（VAD endpoint，~1.6s 静音）判定，
// 不再用固定长防抖打断用户。endpoint 到达后留一个很短的宽限，接住最后一段
// 在途 STT 文本（STT 有 1-2s 延迟），然后立即执行。
const GRACE_AFTER_ENDPOINT_MS = 900; // endpoint 后宽限：接住最后一句 STT
// 兜底：万一 endpoint 信号一直不来（持续背景噪声使静音不足阈值），也别永不执行。
const CMD_FALLBACK_MS = 6_000;
const CMD_BUFFER_MAX_MS = 30_000;
// 免唤醒续聊窗口：Echo 答完后这段时间内，向它提问/下指令无需再喊"echo"。
// 像打电话一样自然多轮；保守判定（仅问句/请求触发）避免背景闲聊误触。
const FOLLOWUP_WINDOW_MS = 14_000;

const agentToolLabel: Record<string, string> = {
  rag_search: "查知识库",
  web_search: "联网搜索",
  generate_artifact: "生成产物",
};

async function buildInlineContext(): Promise<string> {
  try {
    const recent = await listRecentAmbient(30);
    return recent
      .map((s) => `${s.speaker_label ?? s.speaker_id ?? "?"} · ${s.text}`)
      .join("\n");
  } catch {
    return "";
  }
}

interface UseVoiceWakeAgentOptions {
  tts: TtsController;
}

export function useVoiceWakeAgent({
  tts,
}: UseVoiceWakeAgentOptions): {
  handleAmbientText: (text: string) => void;
  handleEndpoint: () => void;
} {
  const appendAssistantReply = useStore((s) => s.appendAssistantReply);
  const patchAssistantReply = useStore((s) => s.patchAssistantReply);
  const addArtifact = useStore((s) => s.addArtifact);
  const beginRun = useStore((s) => s.beginRun);
  const busyRef = useRef(false);
  const lastWakeRef = useRef<{ key: string; at: number } | null>(null);
  // 指令缓冲：唤醒后累积，靠采集层静音 endpoint 判定"说完"再执行。
  const cmdBufRef = useRef<string>("");
  const bufOpenRef = useRef<boolean>(false);
  const endpointSeenRef = useRef<boolean>(false); // 本轮是否已收到静音 endpoint
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const maxWaitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const followUpUntilRef = useRef<number>(0); // 免唤醒续聊窗口截止时间戳
  const currentAbortRef = useRef<(() => void) | null>(null); // 中止当前 run（barge-in）
  const pendingCommandRef = useRef<string | null>(null); // 被打断后排队的新指令
  const runVoiceCommandRef = useRef<((c: string) => Promise<void>) | null>(null);

  const runVoiceCommand = useCallback(
    async (command: string) => {
      // 打断（barge-in）：Echo 还在答时来了新指令 → 立即中止当前回答，把新指令排队，
      // 当前 run 的 finally 会接力执行它。像真人对话一样可随时插话。
      if (busyRef.current) {
        pendingCommandRef.current = command;
        currentAbortRef.current?.();
        message.info({ content: "好，听你的…", key: "echo-bargein", duration: 1.2 });
        return;
      }
      busyRef.current = true;
      // 可中止：注册到全局运行控制（「停止」按钮）+ 暴露给 barge-in。
      const controller = new AbortController();
      const endRun = beginRun(() => {
        controller.abort();
        tts.cancel();
      });
      currentAbortRef.current = () => {
        controller.abort();
        tts.cancel();
      };
      // 语音 @echo 不再造右侧"用户"气泡——用户说的话已作为左侧说话人转写出现，
      // 再造一条会重复（汉宜口今天星期几 / @echo 今天星期几）。这里只在左侧
      // 追加一条 Echo 回复气泡（pending → done/failed），与转写流自然衔接。
      const replyId = appendAssistantReply("", "assistant_reply", undefined, "pending");
      const progressLines: string[] = [];
      let finalAnswer = "";
      let sawFinal = false;
      let sawError = false;
      let sawToolCall = false;

      const renderPending = (body = ""): void => {
        patchAssistantReply(replyId, {
          text: [progressLines.slice(-6).join("\n"), body].filter(Boolean).join("\n\n"),
          status: "pending",
        });
      };

      try {
        renderPending("Echo 正在思考…");
        // 语音触发「今日回顾」：用专用回顾（比泛化 agent 更准、更结构化）。
        if (isDailyRecapCommand(command)) {
          renderPending("正在回顾今天…");
          const r = await getDailyRecap();
          sawFinal = true;
          const answer = r.empty
            ? "今天还没有记录到可回顾的对话或会议。"
            : r.recap_markdown;
          patchAssistantReply(replyId, { text: answer, status: "done" });
          if (tts.enabled && !r.empty) {
            void tts.speak(toSpeakableAnswer(answer), { interrupt: true });
          }
          return;
        }
        const explicitArtifact = extractExplicitArtifactCommand(command);
        if (explicitArtifact) {
          for (let attempt = 1; attempt <= 2 && !sawFinal; attempt += 1) {
            const attemptState: {
              failure: { error: string; stage?: string } | null;
            } = { failure: null };
            await generateArtifactStream(
              {
                artifact_type: explicitArtifact.artifactType,
                brief: explicitArtifact.brief,
              },
              {
                onPhase: ({ msg, phase, total_chars }) => {
                  const suffix =
                    typeof total_chars === "number" && total_chars > 0
                      ? `\n已收到 ${total_chars} 字符`
                      : "";
                  const prefix = attempt > 1 ? `第 ${attempt} 次尝试：` : "";
                  renderPending(`${prefix}${msg || phase || "正在生成产物…"}${suffix}`);
                },
                onLLMChunk: ({ text }) => {
                  renderPending(text.slice(-600) || "正在生成产物…");
                },
                onDone: (artifact) => {
                  sawFinal = true;
                  addArtifact(artifact);
                  const answer = `已生成 ${artifact.artifact_type}：${artifact.title || artifact.artifact_id}`;
                  patchAssistantReply(replyId, {
                    text: answer,
                    artifacts: [artifact],
                    status: "done",
                  });
                  if (tts.enabled) {
                    void tts.speak(answer, { interrupt: true });
                  }
                },
                onError: ({ error, stage }) => {
                  attemptState.failure = { error, stage };
                },
              },
              controller.signal,
            );
            const failure = attemptState.failure;
            if (!sawFinal && failure && attempt < 2) {
              renderPending(
                `云端连接短暂失败，正在自动重试…\n${failure.error}`,
              );
            } else if (!sawFinal && failure) {
              sawError = true;
              patchAssistantReply(replyId, {
                text: `语音生成失败${failure.stage ? `（${failure.stage}）` : ""}：${failure.error}`,
                status: "failed",
              });
            }
          }
          if (!sawFinal && !sawError) {
            patchAssistantReply(replyId, {
              text: "语音生成失败：后端流结束但没有返回产物",
              status: "failed",
            });
          }
          return;
        }
        const inlineContext = await buildInlineContext();
        // 句级流式 TTS：边出文本边逐句播，不再等整段答完才合成（解决"播放比文本晚"）
        const ttsStreamer = createTtsSentenceStreamer(tts);
        await runAgent(
          command,
          { inlineContext, maxIterations: 6, signal: controller.signal },
          {
            // 不主动显示"规划多工具任务（1/6）"——简单问答根本不调工具
            onPlan: () => {},
            onToolCall: ({ name, reason }) => {
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
              progressLines.push(`✓ 已生成 ${artifact.artifact_type}：${artifact.title || artifact.artifact_id}`);
              const existing = useStore
                .getState()
                .conversationEvents.find((e) => e.id === replyId)?.artifacts ?? [];
              patchAssistantReply(replyId, {
                artifacts: [
                  artifact,
                  ...existing.filter((a) => a.artifact_id !== artifact.artifact_id),
                ],
              });
              renderPending();
            },
            onDelta: (text) => {
              finalAnswer += text;
              renderPending(finalAnswer);
              ttsStreamer.push(text);
            },
            onFinal: ({ answer, citations }) => {
              sawFinal = true;
              finalAnswer = answer;
              const cites = (citations ?? [])
                .filter((c) => c.kind === "rag" || c.kind === "web")
                .slice(0, 20);
              patchAssistantReply(replyId, {
                text: answer,
                kind: cites.length > 0 ? "rag_answer" : "assistant_reply",
                citations: cites,
                status: "done",
              });
              ttsStreamer.finalize(toSpeakableAnswer(answer));
            },
            onError: ({ error, stage }) => {
              sawError = true;
              patchAssistantReply(replyId, {
                text: `语音问答失败${stage ? `（${stage}）` : ""}：${error}`,
                status: "failed",
              });
            },
          },
        );

        if (!sawFinal && !sawError) {
          patchAssistantReply(replyId, {
            text: finalAnswer || progressLines.join("\n") || "语音指令已结束。",
            status: "done",
          });
        }
      } catch (e) {
        if (controller.signal.aborted) {
          patchAssistantReply(replyId, {
            text: finalAnswer || "已停止。",
            status: "done",
          });
        } else {
          const raw = e instanceof Error ? e.message : String(e);
          patchAssistantReply(replyId, {
            text: `语音问答失败（连接异常）：${raw}`,
            status: "failed",
          });
        }
      } finally {
        endRun();
        currentAbortRef.current = null;
        busyRef.current = false;
        // 成功答复后开启免唤醒续聊窗口（像打电话一样自然多轮）；失败则不开，
        // 避免在出错语境里继续误触。
        if (sawFinal) {
          followUpUntilRef.current = Date.now() + FOLLOWUP_WINDOW_MS;
        }
        // barge-in：若期间被新指令打断并排了队，接力执行它（走 ref 避免闭包陈旧）。
        const next = pendingCommandRef.current;
        pendingCommandRef.current = null;
        if (next) void runVoiceCommandRef.current?.(next);
      }
    },
    [addArtifact, appendAssistantReply, patchAssistantReply, beginRun, tts],
  );
  // 最新 runVoiceCommand 暴露给 barge-in 接力调用，避免 useCallback 自引用陈旧。
  runVoiceCommandRef.current = runVoiceCommand;

  const dispatchCommand = useCallback(
    (command: string): void => {
      const key = command.toLowerCase();
      const now = Date.now();
      const last = lastWakeRef.current;
      if (last && last.key === key && now - last.at < DUPLICATE_WINDOW_MS) return;
      lastWakeRef.current = { key, at: now };
      void runVoiceCommand(command);
    },
    [runVoiceCommand],
  );

  // 执行累积到的整条指令（清空缓冲 + 计时器）。
  const flushCommandBuffer = useCallback((): void => {
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    if (maxWaitTimerRef.current) clearTimeout(maxWaitTimerRef.current);
    debounceTimerRef.current = null;
    maxWaitTimerRef.current = null;
    const cmd = cmdBufRef.current.trim();
    cmdBufRef.current = "";
    bufOpenRef.current = false;
    endpointSeenRef.current = false;
    if (cmd) dispatchCommand(cmd);
  }, [dispatchCommand]);

  const handleAmbientText = useCallback(
    (text: string): void => {
      const command = extractEchoWakeCommand(text); // 唤醒词 + 同段指令
      const isWake = command !== null || containsWakeWord(text);

      // 免唤醒续聊：Echo 刚答完的窗口内、无唤醒词、缓冲未开，且这句明显是在
      // 向 Echo 提问/下指令 → 当作新指令直接开始（保守判定防背景闲聊误触）。
      const inFollowup =
        !isWake &&
        !bufOpenRef.current &&
        Date.now() < followUpUntilRef.current &&
        isLikelyEchoFollowup(text);
      if (inFollowup) followUpUntilRef.current = 0; // 消费窗口，避免本句重复触发

      // 既不是唤醒、不是续聊、缓冲也没开 → 普通环境转写，忽略。
      if (!isWake && !inFollowup && !bufOpenRef.current) return;

      // 计算本段要追加进缓冲的内容：
      // - 唤醒段：取唤醒词之后的指令（command，可能为空=只喊了 echo）
      // - 续聊段 / 缓冲已开续接：整段都算指令
      const piece = command !== null ? command : isWake ? "" : text.trim();
      if (piece) {
        cmdBufRef.current = cmdBufRef.current
          ? `${cmdBufRef.current} ${piece}`
          : piece;
      }

      // 首次开缓冲：轻提示 + 起兜底计时（防 endpoint 信号始终不来）。
      if (!bufOpenRef.current) {
        bufOpenRef.current = true;
        endpointSeenRef.current = false;
        message.info({ content: "Echo 在听…", key: "echo-listening", duration: 1.5 });
        if (maxWaitTimerRef.current) clearTimeout(maxWaitTimerRef.current);
        maxWaitTimerRef.current = setTimeout(flushCommandBuffer, CMD_BUFFER_MAX_MS);
      }

      // 计时策略：
      // - 已收到静音 endpoint（用户确实停顿够久=说完了）→ 只留很短宽限接住本段，
      //   然后执行（简单回答一停就尽快开始）。
      // - 还没 endpoint → 这只是句间换气，给一个较长的 fallback，等真正的 endpoint
      //   来收尾；绝不因为换气就提前执行。
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
      const wait = endpointSeenRef.current ? GRACE_AFTER_ENDPOINT_MS : CMD_FALLBACK_MS;
      debounceTimerRef.current = setTimeout(flushCommandBuffer, wait);
    },
    [flushCommandBuffer],
  );

  // 采集层静音 endpoint：说话人这一轮"说完了"。此时最后一段 STT 可能仍在途
  // （STT 有 1-2s 延迟），所以不立即执行，而是留一个很短宽限接住它再执行。
  const handleEndpoint = useCallback((): void => {
    if (!bufOpenRef.current) return;
    endpointSeenRef.current = true;
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = setTimeout(flushCommandBuffer, GRACE_AFTER_ENDPOINT_MS);
  }, [flushCommandBuffer]);

  return { handleAmbientText, handleEndpoint };
}

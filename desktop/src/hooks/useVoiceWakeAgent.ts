import { message } from "antd";
import { useCallback, useRef } from "react";
import { generateArtifactStream, listRecentAmbient, runAgent } from "@/api";
import type { TtsController } from "@/hooks/useTtsPlayer";
import { extractExplicitArtifactCommand } from "@/lib/explicitArtifactCommand";
// 唤醒词匹配 + 朗读文本归一统一走 @/lib/voiceWake（单一真源）。
// 早期此处内联过一份小词表，已合并到 lib 并扩充鲁棒性；这里 re-export 保持兼容。
import {
  containsWakeWord,
  extractEchoWakeCommand,
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

  const runVoiceCommand = useCallback(
    async (command: string) => {
      if (busyRef.current) {
        message.info("Echo 正在回答上一条语音指令");
        return;
      }
      busyRef.current = true;
      // 可中止：注册到全局运行控制，「停止」按钮 abort + 停 TTS。
      const controller = new AbortController();
      const endRun = beginRun(() => {
        controller.abort();
        tts.cancel();
      });
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
        busyRef.current = false;
      }
    },
    [addArtifact, appendAssistantReply, patchAssistantReply, beginRun, tts],
  );

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

      // 既不是唤醒、缓冲也没开 → 普通环境转写，忽略。
      if (!isWake && !bufOpenRef.current) return;

      // 计算本段要追加进缓冲的内容：
      // - 唤醒段：取唤醒词之后的指令（command，可能为空=只喊了 echo）
      // - 续聊段（缓冲已开、无唤醒词）：整段都算指令的后续
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

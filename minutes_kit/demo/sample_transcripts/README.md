# sample_transcripts

## meetly_demo.txt
- 场景：三人讨论 meetly 项目第二阶段
- 干净的对话流（带说话人 + 时间戳）
- 用于跟 meetly 项目的输出做产物视觉对比基线
- 期望产物：决议 ≥ 4 / 待办 ≥ 4 / 话题 ≥ 5 / 流程图清晰

## echo_real_meeting.txt
- 来源：`echo/experiments/2026-05-26_real_meeting_e2e/results/transcript.txt`
- 场景：真实工作会议（ASR 原始流，无 diarization）
- 内容：阿尔特发布会 PPT 框架讨论
- **挑战点**：无说话人标识、ASR 噪声较多、口语化重、词转错（如「八页」「八页一页还是有一个十几页」）
- 用来压测：LLM 在噪声场景下的去噪/还原能力，以及 minutes_kit 在「ASR 不标准化输入」下还能稳输出
- 期望产物：流程图能反映「先定文字框架 → 出 PPT 草稿 → 周一彩排 → 周二讲」的脉络

## 使用方式

```bash
# CLI
python -m minutes_kit.cli \
  --transcript demo/sample_transcripts/meetly_demo.txt \
  --out out/run_meetly_demo \
  --participants "A,B,C" \
  --title-hint "meetly 第二阶段产物对齐"

# Demo server：直接粘贴文件内容到浏览器表单
```

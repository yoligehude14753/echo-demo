"""3 节点 LLM 编排：转录 → 正典 JSON。

设计原则（参考 echo backend/app/api/meeting.py::_llm_generate_notes 的体感 + 18-llm-workflow.mdc）：
- 节点最小化：每个节点单一职责，便于独立测试 / 回归
- 结构化输出：全部走 JSON schema，禁用自由文本
- 失败安全：任一节点失败 → 整体抛 ExtractorError，调用方决定降级策略

3 节点 DAG:
    Node A (extract_summary_topics)
        ├──> Node B (extract_decisions_todos)
        └──> Node C (gen_flow_mermaid)
    Node B 和 Node C 可以并行（都只依赖 Node A 的输出）
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from minutes_kit.llm_client import LLMClient
from minutes_kit.models import (
    Decision,
    FlowKind,
    MeetingMinutesData,
    Todo,
    Topic,
    TranscriptTurn,
)


class ExtractorError(RuntimeError):
    """提取失败 — 调用方决定是降级还是终止。"""


# ── Node A：summary + topics + title ──────────────────────────────────────

_NODE_A_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "summary_topics",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "abstract": {"type": "string"},
                "summary_md": {"type": "string"},
                "topics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "time_range": {"type": "string"},
                            "key_points": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["name", "time_range", "key_points"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "abstract", "summary_md", "topics"],
            "additionalProperties": False,
        },
    },
}

_NODE_A_SYSTEM_PROMPT = """你是一位专业的会议记录助手。任务是把会议转录整理成结构化纪要。

输入：一段会议转录（每行 `[HH:MM:SS] 说话人: 内容`）+ 参会人名单 + 可选标题提示。

输出严格 JSON：
- title:        ≤16 字会议标题。若用户给了 title_hint 优先用它；否则从内容里提炼一个简洁主题
- abstract:     ≤120 字一段话总结（用户 5 秒读完能知道讨论了什么、达成了什么共识）
- summary_md:   完整会议纪要 markdown，结构必须包含：
                ## 摘要
                ## 时间线（按时段分点）
                ## 关键结论
                ## 行动项
                ## 参与者
- topics[]:     按主题切分的话题段，每个 topic：
                  - name:        ≤12 字
                  - time_range:  "HH:MM-HH:MM"
                  - key_points:  3-5 条要点

ASR 噪声处理原则：
- 同音错字基于上下文还原
- 数字不确定时跳过或标注"约"
- 完全无法理解的句子直接忽略
- 跳过寒暄、口误、重复

不要瞎编内容；如果转录里没说的事情，绝不在纪要里出现。"""


@dataclass(slots=True)
class _NodeAResult:
    title: str
    abstract: str
    summary_md: str
    topics: list[Topic]


async def _node_a_summary_topics(
    transcript_text: str,
    participants: list[str],
    title_hint: str | None,
    llm: LLMClient,
) -> _NodeAResult:
    participants_str = "、".join(participants) if participants else "未识别"
    title_line = f"标题提示：{title_hint}\n\n" if title_hint else ""
    user = (
        f"{title_line}参会人：{participants_str}\n\n"
        f"会议转录：\n\n{transcript_text}"
    )

    data = await llm.complete_with_schema(
        messages=[
            {"role": "system", "content": _NODE_A_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        schema=_NODE_A_SCHEMA,
        max_tokens=6000,
        temperature=0.3,
    )

    title = str(data.get("title") or "").strip()
    abstract = str(data.get("abstract") or "").strip()
    summary_md = str(data.get("summary_md") or "").strip()
    topics_raw = data.get("topics") or []

    if not title or not summary_md:
        raise ExtractorError(
            f"Node A 输出缺少必填字段: title={title!r} summary_md_len={len(summary_md)}"
        )

    topics = [Topic.from_dict(t) for t in topics_raw if isinstance(t, dict)]
    return _NodeAResult(
        title=title[:32],
        abstract=abstract[:240],
        summary_md=summary_md,
        topics=topics,
    )


# ── Node B：decisions + todos ─────────────────────────────────────────────

_NODE_B_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "decisions_todos",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "statement": {"type": "string"},
                            "rationale": {"type": "string"},
                            "impact": {"type": "string"},
                        },
                        "required": ["statement", "rationale", "impact"],
                        "additionalProperties": False,
                    },
                },
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "owner": {"type": "string"},
                            "due": {"type": "string"},
                            "priority": {
                                "type": "string",
                                "enum": ["high", "med", "low"],
                            },
                        },
                        "required": ["task", "owner", "due", "priority"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["decisions", "todos"],
            "additionalProperties": False,
        },
    },
}

_NODE_B_SYSTEM_PROMPT = """你的任务：从会议纪要里提取「决议」和「待办」两类结构化记录。

判定边界：
- 决议 = 会议中达成的共识、立场、规则、原则；是「事情应该这么做」的陈述
- 待办 = 会议中明确分配给某人/某角色去做的具体动作；是「谁要在何时之前做完什么」的承诺
- 一件事如果既有共识又有动作，**两边都要出**：决议表达共识，待办表达执行

输出严格 JSON：
{
  "decisions": [
    {
      "statement": "≤40字 决议陈述，关键名词/数字/角色用 **加粗**（markdown），如 '**Word 模板** 由 B 负责，**周三前** 出第一版'",
      "rationale": "≤40字 为什么这么决定（若纪要里没说写空字符串）",
      "impact":    "≤40字 这个决议会影响什么（若纪要里没说写空字符串）"
    }
  ],
  "todos": [
    {
      "task":     "≤40字 具体动作",
      "owner":    "负责人名字；未指派写「未指派」",
      "due":      "截止日期文本（如「周五前」「2026-06-01」「TBD」）；不确定写「TBD」",
      "priority": "high | med | low"
    }
  ]
}

优先级判定参考：
- high:  影响下次会议 / 关键决策依赖 / 24h 内需完成
- med:   本周内 / 不阻塞他人但需推进（默认档）
- low:   不紧急 / 仅作记录

绝不编造未在纪要里出现的决议或行动。宁可输出空数组也别瞎编。"""


@dataclass(slots=True)
class _NodeBResult:
    decisions: list[Decision]
    todos: list[Todo]


async def _node_b_decisions_todos(
    summary_md: str,
    topics: list[Topic],
    llm: LLMClient,
) -> _NodeBResult:
    topics_brief = "\n".join(
        f"- {t.name}（{t.time_range}）: {'; '.join(t.key_points)}" for t in topics
    )
    user = f"会议纪要正文：\n\n{summary_md}\n\n话题骨架：\n{topics_brief}"

    data = await llm.complete_with_schema(
        messages=[
            {"role": "system", "content": _NODE_B_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        schema=_NODE_B_SCHEMA,
        max_tokens=4000,
        temperature=0.2,
    )

    decisions = [Decision.from_dict(x) for x in (data.get("decisions") or []) if isinstance(x, dict)]
    todos = [Todo.from_dict(x) for x in (data.get("todos") or []) if isinstance(x, dict)]

    # 过滤掉 statement / task 为空的脏数据
    decisions = [d for d in decisions if d.statement.strip()]
    todos = [t for t in todos if t.task.strip()]

    return _NodeBResult(decisions=decisions, todos=todos)


# ── Node C：flow_mermaid + flow_kind ──────────────────────────────────────

_NODE_C_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "flow_mermaid",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "flow_kind": {
                    "type": "string",
                    "enum": ["flowchart", "sequenceDiagram", "mindmap", "timeline"],
                },
                "flow_mermaid": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["flow_kind", "flow_mermaid", "rationale"],
            "additionalProperties": False,
        },
    },
}

_NODE_C_SYSTEM_PROMPT = """你的任务：用 Mermaid 语法画一张「会议可视化流程图」，让读者 10 秒内看懂这场会议的脉络。

第一步：根据内容自由选最合适的图种（flow_kind）：
- flowchart       适合：决策因果链路、问题→讨论→结论→行动的推理过程
- sequenceDiagram 适合：参与者之间的对话/请求/响应、强角色互动场景
- mindmap         适合：以一个核心议题分层展开的话题结构
- timeline        适合：以时间为骨架的事件流（每个事件挂在时间点上）

第二步：写 Mermaid 源码，严格遵守以下约束（破坏任一条都会让图无法渲染）：
1. 首行必须是 "flowchart TD" / "sequenceDiagram" / "mindmap" / "timeline" 之一
2. 节点 ID 用 camelCase，**绝对禁止空格、中文、特殊字符**
3. 节点标签可以中文，包在 [] 或 () 里（如 nodeA["开始讨论 Word 模板"]）
4. 节点数量：3 ≤ n ≤ 12
5. 不要用 style、click、subgraph、HTML 实体（&lt; &gt; 等）、注释（%%）
6. 不要给节点设颜色
7. mindmap 中根节点写 root((中心议题))

第三步：用 rationale 字段（≤30字）说明你为什么选这种图种。

输出严格 JSON：
{
  "flow_kind":    "...",
  "flow_mermaid": "源码字符串（含必要的换行）",
  "rationale":    "..."
}

样例（flowchart）：
{
  "flow_kind": "flowchart",
  "flow_mermaid": "flowchart TD\\n  start([会议开始])\\n  word[讨论 Word 模板]\\n  excel[讨论 Excel 待办格式]\\n  decide{统一规范？}\\n  deploy[周一部署 staging]\\n  start --> word --> decide\\n  start --> excel --> decide\\n  decide -->|是| deploy",
  "rationale": "决策因果链路最适合"
}"""


_MERMAID_FIRST_LINE_RE = re.compile(
    r"^(flowchart\s+\w+|sequenceDiagram|mindmap|timeline)\b"
)


@dataclass(slots=True)
class _NodeCResult:
    flow_kind: FlowKind
    flow_mermaid: str
    rationale: str


async def _node_c_flow(
    topics: list[Topic],
    decisions: list[Decision],
    todos: list[Todo],
    llm: LLMClient,
) -> _NodeCResult:
    topics_brief = "\n".join(
        f"- {t.name}（{t.time_range}）: {'; '.join(t.key_points)}" for t in topics
    )
    decisions_brief = "\n".join(f"- {d.statement}" for d in decisions)
    todos_brief = "\n".join(
        f"- [{t.priority}] {t.task} @ {t.owner or '未指派'} (due: {t.due or 'TBD'})"
        for t in todos
    )
    user = (
        f"会议话题骨架：\n{topics_brief}\n\n"
        f"决议清单：\n{decisions_brief or '(无)'}\n\n"
        f"待办清单：\n{todos_brief or '(无)'}\n\n"
        f"请输出最能反映这场会议脉络的 Mermaid 图。"
    )

    data = await llm.complete_with_schema(
        messages=[
            {"role": "system", "content": _NODE_C_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        schema=_NODE_C_SCHEMA,
        max_tokens=2000,
        temperature=0.4,
    )

    flow_kind_raw = str(data.get("flow_kind") or "flowchart")
    flow_mermaid = str(data.get("flow_mermaid") or "").strip()
    rationale = str(data.get("rationale") or "")[:80]

    if flow_kind_raw not in ("flowchart", "sequenceDiagram", "mindmap", "timeline"):
        flow_kind_raw = "flowchart"

    flow_mermaid = _sanitize_mermaid(flow_mermaid, flow_kind_raw)

    return _NodeCResult(
        flow_kind=flow_kind_raw,  # type: ignore[arg-type]
        flow_mermaid=flow_mermaid,
        rationale=rationale,
    )


def _sanitize_mermaid(src: str, kind: str) -> str:
    """清洗 LLM 输出的 mermaid 源码 —— 保证可渲染。

    1. 去除可能的 ```mermaid``` 围栏
    2. 校验首行符合图种约束；不符合 → 抛 ExtractorError 让上层兜底
    3. 去除 style/click/subgraph 等高级特性（防 LLM 私自加上）
    4. 长度上限 4000 字符
    """
    if not src:
        raise ExtractorError("Node C: flow_mermaid 为空")

    # 剥围栏
    if src.startswith("```"):
        lines = src.split("\n")
        if len(lines) > 2:
            end = -1 if lines[-1].strip().startswith("```") else len(lines)
            src = "\n".join(lines[1:end]).strip()

    if len(src) > 4000:
        src = src[:4000]

    # 检查首行
    first_line = src.split("\n", 1)[0].strip()
    if not _MERMAID_FIRST_LINE_RE.match(first_line):
        raise ExtractorError(
            f"Node C: 首行非法 (期望 flowchart TD / sequenceDiagram / mindmap / timeline)；"
            f"实际首行: {first_line!r}"
        )

    # 删 style / click / subgraph 行（meta-defensive）
    lines = src.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("style ", "click ", "%%", "linkStyle ")):
            continue
        # subgraph 不一定有害但容易渲染异常，去掉但保留内部内容
        if stripped.startswith("subgraph ") or stripped == "end":
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


# ── 公开入口 ────────────────────────────────────────────────────────────


async def extract_minutes(
    transcript: list[TranscriptTurn],
    *,
    llm_client: LLMClient,
    participants: list[str] | None = None,
    title_hint: str | None = None,
    minutes_id: str | None = None,
) -> MeetingMinutesData:
    """完整执行 3 节点编排，返回正典 JSON。

    任一节点失败 → 抛 ExtractorError；上层（orchestrator）决定是降级还是终止。
    """
    if not transcript:
        raise ExtractorError("transcript 为空")

    # 推断参会人
    if not participants:
        speakers = sorted({t.speaker for t in transcript if t.speaker and t.speaker != "?"})
        participants = speakers or ["未识别"]

    # 推断时间范围
    from_time = transcript[0].ts or ""
    to_time = transcript[-1].ts or ""

    transcript_text = _format_transcript(transcript)
    logger.info(
        f"[extractor] start: {len(transcript)} turns, "
        f"{len(participants)} participants, transcript_chars={len(transcript_text)}"
    )

    # Node A 必须先跑（B 和 C 依赖它的输出）
    a = await _node_a_summary_topics(transcript_text, participants, title_hint, llm_client)
    logger.info(f"[extractor] Node A done: title={a.title!r} topics={len(a.topics)}")

    # Node B 和 Node C 并行
    b_task = asyncio.create_task(_node_b_decisions_todos(a.summary_md, a.topics, llm_client))
    c_task = asyncio.create_task(_node_c_flow(a.topics, [], [], llm_client))

    # 等 B 先完成，把 decisions+todos 喂给 C 重试（如果 C 的初版图脉络比较单薄）
    # 这里为简化：让 B 和 C 并行，C 拿不到 B 也能基于 topics 画图
    b_result, c_result = await asyncio.gather(b_task, c_task, return_exceptions=True)

    if isinstance(b_result, Exception):
        raise ExtractorError(f"Node B 失败: {b_result}") from b_result
    if isinstance(c_result, Exception):
        # C 失败不算硬故障：用默认 flowchart 占位
        logger.warning(f"[extractor] Node C 失败，使用占位流程图: {c_result}")
        c_result = _NodeCResult(
            flow_kind="flowchart",
            flow_mermaid=_placeholder_flow(a.topics),
            rationale="LLM 流程图生成失败，使用 topics 占位",
        )

    logger.info(
        f"[extractor] Node B done: decisions={len(b_result.decisions)} todos={len(b_result.todos)}"
    )
    logger.info(f"[extractor] Node C done: kind={c_result.flow_kind} chars={len(c_result.flow_mermaid)}")

    return MeetingMinutesData(
        minutes_id=minutes_id or uuid.uuid4().hex[:12],
        title=a.title,
        from_time=from_time,
        to_time=to_time,
        participants=participants,
        abstract=a.abstract,
        summary_md=a.summary_md,
        decisions=b_result.decisions,
        todos=b_result.todos,
        topics=a.topics,
        flow_mermaid=c_result.flow_mermaid,
        flow_kind=c_result.flow_kind,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _format_transcript(transcript: list[TranscriptTurn]) -> str:
    """每行 `[HH:MM:SS] speaker: text`。"""
    parts = []
    for turn in transcript:
        ts = turn.ts
        # 若 ts 是 ISO8601，截 HH:MM:SS
        if "T" in ts:
            ts = ts.split("T", 1)[1][:8]
        parts.append(f"[{ts}] {turn.speaker}: {turn.text}")
    return "\n".join(parts)


def _placeholder_flow(topics: list[Topic]) -> str:
    """C 节点失败时的兜底流程图：把 topics 串成 flowchart。"""
    if not topics:
        return "flowchart TD\n  start([会议开始]) --> done([会议结束])"
    lines = ["flowchart TD", "  start([会议开始])"]
    for i, t in enumerate(topics):
        nid = f"t{i}"
        label = t.name.replace("[", "").replace("]", "").replace('"', "")
        lines.append(f'  {nid}["{label}"]')
    lines.append("  done([会议结束])")
    # 串联
    prev = "start"
    for i in range(len(topics)):
        lines.append(f"  {prev} --> t{i}")
        prev = f"t{i}"
    lines.append(f"  {prev} --> done")
    return "\n".join(lines)

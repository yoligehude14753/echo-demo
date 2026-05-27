"""共享 fixture：mock LLMClient + 示例数据。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from minutes_kit.models import Decision, MeetingMinutesData, Todo, Topic


class MockLLMClient:
    """根据 system prompt 内容匹配应返回哪类输出的 mock client。

    保留收到的 messages，方便单测断言 prompt 构造。
    """

    def __init__(self, *, fail_node: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_node = fail_node  # "A" / "B" / "C" 模拟单节点失败

    async def complete(self, messages, *, model=None, max_tokens=16000, temperature=0.3):
        self.calls.append({"messages": messages, "kind": "text"})
        return "mock-text"

    async def complete_with_schema(
        self,
        messages,
        schema,
        *,
        model=None,
        max_tokens=4000,
        temperature=0.2,
    ):
        self.calls.append({"messages": messages, "schema": schema, "kind": "schema"})

        sys_prompt = ""
        for m in messages:
            if m.get("role") == "system":
                sys_prompt = m.get("content", "")
                break

        if "会议记录助手" in sys_prompt:
            if self.fail_node == "A":
                raise RuntimeError("mock: Node A fail")
            return _NODE_A_FIXTURE
        if "「决议」和「待办」" in sys_prompt:
            if self.fail_node == "B":
                raise RuntimeError("mock: Node B fail")
            return _NODE_B_FIXTURE
        if "Mermaid 语法画一张" in sys_prompt:
            if self.fail_node == "C":
                raise RuntimeError("mock: Node C fail")
            return _NODE_C_FIXTURE

        raise AssertionError(f"未预期的 system prompt: {sys_prompt[:60]!r}")


_NODE_A_FIXTURE: dict[str, Any] = {
    "title": "周三例会",
    "abstract": "本次会议确定了产物自动化的 4 件套规范，并明确了周五前完成 demo，周一部署 staging。",
    "summary_md": (
        "本次会议围绕 **第二阶段产物范围**、任务分工以及交付时间表展开讨论，"
        "确定了 **Word/Excel/PPT/HTML 四种载体**的优先级与负责人安排，"
        "并就 **周一上午 staging 部署** 达成一致，内容如下：\n\n"
        "- **产物范围对齐**\n"
        "  - **四种载体**：Word、Excel、PPT、HTML 四种纪要形态同步推进\n"
        "    - **Word 模板**：作为 PPT 与 Excel 复用的样式基础，由 B 负责出 **第一版**\n"
        "    - **Excel sheet**：待办专属独立 sheet，由 C 牵头\n"
        "    - **PPT 模板**：复用 Anthropic skill 自带模板，节省自研成本\n"
        "    - **HTML 样式**：使用自有样式，与 Word 设计 token 保持一致\n"
        "  - **Word 优先级最高**，原因是模板风格统一会反向影响 PPT/Excel\n"
        "- **分工与时间表**\n"
        "  - **B 负责 Word**：周三前出第一版，周四组内 review\n"
        "  - **C 负责 Excel**：周五前完成 sheet 拆分与待办字段\n"
        "  - **A 负责协调**：联系 ops 安排周一 staging 部署窗口\n"
        "- **风险与下一步**\n"
        "  - 当前 extract_decisions prompt 输出不稳定，A 本周内调通\n"
        "  - 周五前完成 demo，周末预留缓冲，周一上午部署 staging"
    ),
    "topics": [
        {
            "name": "产物范围对齐",
            "time_range": "10:00-10:01",
            "key_points": ["四件套：Word/Excel/PPT/HTML", "Word 优先做模板"],
        },
        {
            "name": "分工",
            "time_range": "10:01-10:02",
            "key_points": ["Word B", "Excel C", "PPT 用 skill 模板", "HTML 自有样式"],
        },
        {
            "name": "时间表",
            "time_range": "10:02-10:04",
            "key_points": ["周五前完成 demo", "周一部署 staging"],
        },
    ],
}

_NODE_B_FIXTURE: dict[str, Any] = {
    "decisions": [
        {
            "statement": "**Word 模板** 由 B 负责，**周三前** 出第一版",
            "rationale": "Word 是 PPT/Excel 复用基础",
            "impact": "后续三类产物风格统一",
        },
        {
            "statement": "Excel 待办 **独立 sheet**，C **周五前** 完成",
            "rationale": "便于跨会议跟踪",
            "impact": "",
        },
        {
            "statement": "PPT 复用 **Anthropic skill** 自带模板",
            "rationale": "skill 模板已够用",
            "impact": "节省自研成本",
        },
        {
            "statement": "**周一上午** 部署 staging",
            "rationale": "周五出 demo，周末间隔留缓冲",
            "impact": "",
        },
    ],
    "todos": [
        {"task": "出 Word 模板第一版", "owner": "B", "due": "周三前", "priority": "high"},
        {"task": "Excel 待办 sheet 开发", "owner": "C", "due": "周五前", "priority": "high"},
        {"task": "联系 ops 安排周一 staging 部署", "owner": "A", "due": "本周内", "priority": "med"},
        {"task": "调通 extract_decisions prompt", "owner": "A", "due": "TBD", "priority": "med"},
    ],
}

_NODE_C_FIXTURE: dict[str, Any] = {
    "flow_kind": "flowchart",
    "flow_mermaid": (
        "flowchart TD\n"
        "  start([会议开始])\n"
        "  scope[第二阶段范围对齐]\n"
        "  word[Word 模板 B]\n"
        "  excel[Excel sheet C]\n"
        "  ppt[PPT skill 模板]\n"
        "  html[HTML 自有样式]\n"
        '  demo{周五前 demo 完整?}\n'
        "  staging[周一 staging 部署]\n"
        "  start --> scope\n"
        "  scope --> word\n"
        "  scope --> excel\n"
        "  scope --> ppt\n"
        "  scope --> html\n"
        "  word --> demo\n"
        "  excel --> demo\n"
        "  ppt --> demo\n"
        "  html --> demo\n"
        "  demo -->|是| staging"
    ),
    "rationale": "决策因果链路",
}


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def sample_minutes_data() -> MeetingMinutesData:
    """干净的 MeetingMinutesData 对象（不经 LLM，直接构造）。"""
    return MeetingMinutesData(
        minutes_id="abc123def456",
        title="周三例会",
        from_time="2026-05-27T10:00:00+08:00",
        to_time="2026-05-27T10:04:20+08:00",
        participants=["A", "B", "C"],
        abstract="本次会议确定了 4 件套规范并分工，周五前完成 demo。",
        summary_md="## 摘要\n\n讨论产物分工。\n\n## 时间线\n\n- 10:00 开始\n- 10:04 结束\n",
        decisions=[
            Decision(
                statement="Word 模板由 B 负责",
                rationale="Word 是复用基础",
                impact="后续三类产物风格统一",
            ),
            Decision(statement="周一 staging 部署", rationale=None, impact=None),
        ],
        todos=[
            Todo(task="出 Word 模板", owner="B", due="周三前", priority="high"),
            Todo(task="Excel sheet", owner="C", due="周五前", priority="high"),
            Todo(task="联系 ops", owner="A", due=None, priority="med"),
        ],
        topics=[
            Topic(
                name="分工",
                time_range="10:01-10:02",
                key_points=["Word B", "Excel C", "HTML 自有"],
            ),
        ],
        flow_mermaid=(
            "flowchart TD\n"
            "  start([会议开始])\n"
            "  word[Word 模板]\n"
            "  excel[Excel sheet]\n"
            "  staging[staging 部署]\n"
            "  start --> word\n"
            "  start --> excel\n"
            "  word --> staging\n"
            "  excel --> staging"
        ),
        flow_kind="flowchart",
        created_at="2026-05-27T20:30:00+08:00",
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def sample_transcript_text() -> str:
    return (
        "[10:00:00] A: 我们对一下产物自动化\n"
        "[10:00:10] B: 我建议 Word 先做模板\n"
        "[10:00:20] A: 同意，B 你来负责 Word\n"
        "[10:00:30] B: 好\n"
        "[10:00:40] C: Excel 我来\n"
    )

"""Intent schema：@路由意图（2026-05 修订 + P4-M3 扩展 + ADR-012 agent task）。

PR-16 / m5-t5：用户在前端聊天框输入"@查英伟达营收"等，
调 /intent/route → LLM 分类返回 IntentResult{kind, params, confidence}
→ 前端按 kind 触发对应业务（产物/搜索/纪要）。

设计修订：
- 2026-05：删除 ``start_meeting`` / ``end_meeting`` 意图（由 UI 状态机统一控制），
  ``summarize_meeting`` 保留作为手动 finalize 入口。
- 2026-05-28 P4-M3：新增 ``generate_markdown`` / ``generate_pdf`` / ``generate_txt``，
  对应 SkillExecutor 的新产物类型（markdown/pdf/txt）。
- 2026-05-28 P4-fix-rag-chat（本次）：
  · 用户痛点：上传 PDF 后输入"请基于附件回答（XX.pdf）"被分到 chat → 走兜底
    toast 把用户原文复述+TTS，**完全没用 PDF**。根因是 intent router 把
    没 ``@`` 前缀的句子硬归 chat，且 chat 兜底链路不调 RAG。
  · 修复：加 ``chat_no_rag`` 显式逃生意图（``@chat`` 前缀走纯闲聊不查 RAG）；
    ``chat`` 默认在 UI 层走 RAG（CommandBar 改成调 ragAsk），让 PDF 真生效；
    keyword_route 加 "基于附件 / 给我介绍 / 是什么" 等强 RAG 信号。

12 类意图：
- search_web        : @查 / @搜 / @最新（联网检索）
- search_rag        : @回忆 / @上次会议 / @找文档 / "基于附件回答" / 问句默认（本地知识库检索）
- generate_html     : @生成 HTML / @报告
- generate_pptx     : @生成 PPT / @幻灯片
- generate_xlsx     : @生成 Excel / @表格 / @财务模型
- generate_word     : @生成 Word / @文档
- generate_markdown : @生成 Markdown / @笔记 / @md
- generate_pdf      : @生成 PDF / @简历
- generate_txt      : @生成 TXT / @文本 / @纯文本
- summarize_meeting : @总结当前会议 / @生成纪要
- agent_task        : 后台执行任务（长任务 / 文件操作 / GUI / 浏览器 / 深度调研）
- chat_no_rag       : @chat 前缀（明示不用 RAG，纯 LLM 闲聊）
- chat              : 兜底，不带 @ 且不像问句也不像 RAG 检索请求
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_INTENT_TEXT_CHARS = 32_000
MAX_RESOURCE_ID_CHARS = 256

IntentKind = Literal[
    "search_web",
    "search_rag",
    "generate_html",
    "generate_pptx",
    "generate_xlsx",
    "generate_word",
    "generate_markdown",
    "generate_pdf",
    "generate_txt",
    "summarize_meeting",
    "agent_task",
    "chat_no_rag",
    "chat",
]

SUPPORTED_INTENTS: frozenset[str] = frozenset(
    [
        "search_web",
        "search_rag",
        "generate_html",
        "generate_pptx",
        "generate_xlsx",
        "generate_word",
        "generate_markdown",
        "generate_pdf",
        "generate_txt",
        "summarize_meeting",
        "agent_task",
        "chat_no_rag",
        "chat",
    ]
)

# generate_* 意图 → SkillExecutor artifact_type（canonical kind）
INTENT_TO_ARTIFACT_TYPE: dict[IntentKind, str] = {
    "generate_html": "html",
    "generate_pptx": "pptx",
    "generate_xlsx": "xlsx",
    "generate_word": "word",
    "generate_markdown": "markdown",
    "generate_pdf": "pdf",
    "generate_txt": "txt",
}


class IntentResult(BaseModel):
    kind: IntentKind
    # P4-fix（2026-05-28）：confidence 现在是 Optional。
    # 历史上 "无 @ 前缀 → chat" 路径硬编码返回 confidence=1.0，给用户
    # 一个 "置信度 100%" 的虚假百分比——但这条路径根本没跑分类器，
    # 数字没有实质语义。改为 None 表示"该路径不产生置信度"，前端按 null 处理。
    # 真正经过关键字/LLM 分类的路径仍返回有意义的 float（0.0~1.0）。
    confidence: float | None = Field(ge=0.0, le=1.0, default=None)
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class IntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_INTENT_TEXT_CHARS)
    current_meeting_id: str | None = Field(
        default=None,
        max_length=MAX_RESOURCE_ID_CHARS,
    )  # 提供给 summarize_meeting 用


def parse_at_prefix(text: str) -> str | None:
    """简单解析：'@xxx ...' → 'xxx'；非 @ 开头 → None。"""
    s = text.lstrip()
    if not s.startswith("@"):
        return None
    rest = s[1:].lstrip()
    if not rest:
        return None
    # 取第一个 token（空格/标点切分）
    for i, ch in enumerate(rest):
        if ch in {" ", "\t", "，", "。", ",", "."}:
            return rest[:i]
    return rest


# 极简正则路由：在 LLM 不可达 / 高置信度场景下兜底
# 注意：键统一为小写，匹配时也 .lower()，所以"PDF"等大写写法用 "pdf"。
_KEYWORD_HINTS: dict[str, IntentKind] = {
    # search
    "查": "search_web",
    "搜": "search_web",
    "最新": "search_web",
    "新闻": "search_web",
    "回忆": "search_rag",
    "上次": "search_rag",
    "之前": "search_rag",
    "找文档": "search_rag",
    "找资料": "search_rag",
    # generate（顺序敏感：先匹配更具体的关键字，再到通用的）
    "markdown": "generate_markdown",
    "md ": "generate_markdown",  # 带空格避免吞掉 "model" 等
    "笔记": "generate_markdown",
    "pdf": "generate_pdf",
    "简历": "generate_pdf",
    "txt": "generate_txt",
    "纯文本": "generate_txt",
    "文本文件": "generate_txt",
    "ppt": "generate_pptx",
    "幻灯": "generate_pptx",
    "幻灯片": "generate_pptx",
    "excel": "generate_xlsx",
    "表格": "generate_xlsx",
    "财务模型": "generate_xlsx",
    "word": "generate_word",
    "文档": "generate_word",
    "报告": "generate_html",
    "html": "generate_html",
    "网页": "generate_html",
    "总结会议": "summarize_meeting",
    "生成纪要": "summarize_meeting",
    "总结当前": "summarize_meeting",
    "纪要": "summarize_meeting",
}


# P4-fix-rag-chat（2026-05-28）：强 RAG 信号词组。命中之一即视为"用户想查/问
# 本地知识库 / 附件"，强制路由到 search_rag（confidence=0.9）。
#
# 设计目的：堵住"请基于附件回答（XX.pdf）"这类没有 @ 前缀但语义明确指向
# 已上传文档的输入被旧逻辑硬归为 chat → 走兜底 toast 的坑。
#
# 两组关键词的"任一组命中"判定（OR 而非"两组都要中"），覆盖更广：
#   1. 带"基于/根据/参考/结合/按照"等指向性副词 → 大概率指向附件
#   2. 带"附件/文档/资料/PDF/材料/手册/这份/这个文件"等显式提及上传产物
# 任一短语命中即视为 RAG 强信号。
_STRONG_RAG_PHRASES: tuple[str, ...] = (
    "基于附件",
    "根据附件",
    "参考附件",
    "结合附件",
    "看下附件",
    "看看附件",
    "基于这个文档",
    "基于这份文档",
    "基于这份资料",
    "基于这份材料",
    "基于这份 pdf",
    "基于这份pdf",
    "基于上传的",
    "根据文档",
    "根据资料",
    "根据这份",
    "根据上传",
    "参考文档",
    "参考资料",
    "参考这份",
    "依据文档",
    "结合文档",
    "结合资料",
    "结合手册",
    "结合这份",
    "看下文档",
    "看下这份",
    "从文档里",
    "从资料里",
    "从附件里",
    "从这份",
    "在附件里",
    "在文档里",
    "在资料里",
    "手册里",
    "手册中",
    "产品手册",
)

# 问句信号：含问号 / 含问句词的输入默认走 RAG（"默认查本地知识"语义）。
#
# 注意：单个 token "什么 / 怎么 / 为什么 / 介绍 / 说说 / 讲讲" 等独立出现就足够；
# 不要求"问号 + 关键字 两个都要"，避免英文输入 / 中文标点缺失的句子被漏掉。
_QUESTION_MARKS: tuple[str, ...] = ("?", "?")
_QUESTION_WORDS: tuple[str, ...] = (
    "什么",
    "为什么",
    "为何",
    "怎么",
    "怎样",
    "如何",
    "哪些",
    "哪个",
    "介绍",
    "说说",
    "讲讲",
    "解释",
    "概括",
    "总结一下",
    "是啥",
    "是多少",
    "区别",
    "对比",
    "优缺点",
    "功能",
    "特点",
)

# chat_no_rag 显式逃生短语（与 _KEYWORD_HINTS["@chat"] 配合）：
# 当输入显式以 "@chat" / "chat:" / "闲聊:" 开头时，即便后面带问号也归 chat_no_rag。
_CHAT_NO_RAG_MARKERS: tuple[str, ...] = ("@chat", "chat:", "闲聊:", "聊一下:")


def _strong_rag_hit(text: str) -> bool:
    """检测强 RAG 信号词组（不区分大小写）。"""
    lower = text.lower()
    return any(phrase in lower for phrase in _STRONG_RAG_PHRASES)


def _is_question(text: str) -> bool:
    """检测输入是不是问句（含问号 or 含问句词）。

    判定要求"内容像问句"而不仅"看到问号"：一些短陈述句也含问号（如 "...?"），
    但同时不含问句词；视为问句即可走 RAG（用户真的在问什么 → 走 RAG 不亏）。
    """
    if any(mark in text for mark in _QUESTION_MARKS):
        return True
    return any(word in text for word in _QUESTION_WORDS)


def _chat_no_rag_explicit(text: str) -> bool:
    lower = text.lstrip().lower()
    return any(lower.startswith(marker) for marker in _CHAT_NO_RAG_MARKERS)


def keyword_route(text: str) -> tuple[IntentKind, float] | None:
    """关键字快速路由：命中明确 token 返回高置信度，避免每次调 LLM。

    优先级（从高到低）：
      1. ``@chat`` 显式逃生 → ``chat_no_rag``（用户明确不要 RAG）
      2. 强 RAG 信号词组（"基于附件" / "产品手册里" / ...）→ ``search_rag``
      3. 现有 _KEYWORD_HINTS 关键字（生成产物 / web 搜索 / 纪要 / 等）
      4. 问句信号（含问号 / 含 "什么 / 介绍" 等）→ ``search_rag``（默认走 RAG）
    """
    # 1. chat_no_rag 显式 escape
    if _chat_no_rag_explicit(text):
        return "chat_no_rag", 0.95

    # 2. 强 RAG 信号 → 0.9 高置信度
    if _strong_rag_hit(text):
        return "search_rag", 0.9

    # 3. 现有 keyword token 匹配（小写）
    lower = text.lower()
    for kw, kind in _KEYWORD_HINTS.items():
        if kw in lower:
            return kind, 0.85

    # 4. 问句信号兜底（默认走 RAG）
    if _is_question(text):
        return "search_rag", 0.7

    return None

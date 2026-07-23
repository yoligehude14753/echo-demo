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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_INTENT_TEXT_CHARS = 32_000
MAX_RESOURCE_ID_CHARS = 256
MAX_INTENT_CONTEXT_ITEMS = 24

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

# 所有可由聊天入口触发的内置动作。keyword_route 只能把它们作为给
# 主模型的候选，绝不能作为执行授权。
BUILTIN_SKILL_INTENTS: frozenset[str] = frozenset(
    [
        "search_web",
        "search_rag",
        *INTENT_TO_ARTIFACT_TYPE.keys(),
        "summarize_meeting",
    ]
)
ExecutionTarget = Literal[
    "builtin_skill",
    "claude_code_runtime",
    "conversation",
    "clarification",
]


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


class BuiltinIntentPlan(BaseModel):
    """所有用户聊天入口共享的、主模型产出的严格执行计划。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=1_000)
    execution_target: ExecutionTarget
    builtin_intent: str | None = Field(default=None, max_length=64)
    available_context: list[str] = Field(max_length=MAX_INTENT_CONTEXT_ITEMS)
    steps: list[str] = Field(min_length=1, max_length=16)
    critical_constraints: list[str] = Field(max_length=16)
    missing_constraints: list[str] = Field(max_length=12)
    assumptions: list[str] = Field(max_length=12)
    clarification_questions: list[str] = Field(max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)
    execution_authorized: bool

    @field_validator("execution_target", mode="before")
    @classmethod
    def _normalize_legacy_conversation_target(cls, value: object) -> object:
        """Accept one former wire value while emitting the canonical target.

        A deployed main-model prompt can briefly retain the previous label after
        the source prompt changes. Normalizing it here keeps the plan gate
        fail-closed for every other value without rejecting a conversation
        request during a rolling update.
        """

        return "conversation" if value == "conversational_response" else value

    @model_validator(mode="after")
    def _validate_target(self) -> BuiltinIntentPlan:
        if self.execution_target == "builtin_skill":
            if self.builtin_intent not in BUILTIN_SKILL_INTENTS:
                raise ValueError("builtin_skill requires a supported builtin_intent")
        elif self.builtin_intent is not None:
            raise ValueError("only builtin_skill may select builtin_intent")
        if self.execution_target == "clarification" and self.execution_authorized:
            raise ValueError("clarification cannot be execution_authorized")
        return self


class IntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_INTENT_TEXT_CHARS)
    current_meeting_id: str | None = Field(
        default=None,
        max_length=MAX_RESOURCE_ID_CHARS,
    )  # 提供给 summarize_meeting 用
    available_context: list[str] = Field(
        default_factory=list,
        max_length=MAX_INTENT_CONTEXT_ITEMS,
    )


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


# 关键词只生成给主模型的候选提示，绝不是路由或执行兜底。
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
_QUESTION_MARKS: tuple[str, ...] = ("?", "？")
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

# 明确产出请求不应交给模型猜。这些名词只有与产出动作/请求语气
# 组合时才触发，避免把“这份报告说了什么？”错路由为生成。
_OUTPUT_NOUNS: tuple[str, ...] = (
    "深度研究",
    "调研报告",
    "研究报告",
    "分析报告",
    "日报",
    "周报",
    "月报",
    "年报",
    "汇报材料",
    "调研",
    "报告",
    "方案",
    "提案",
    "白皮书",
    "简报",
    "文档",
)
_OUTPUT_ACTIONS: tuple[str, ...] = (
    "做",
    "写",
    "生成",
    "制作",
    "撰写",
    "编写",
    "整理成",
    "总结成",
    "输出",
    "产出",
    "形成",
    "出一份",
    "出一个",
    "准备一份",
)
_OUTPUT_REQUEST_MARKERS: tuple[str, ...] = (
    "请",
    "帮我",
    "给我",
    "替我",
    "麻烦",
    "需要你",
    "希望你",
    "我要",
    "我想要",
    "能否",
    "可以帮",
)
_QUESTION_ONLY_MARKERS: tuple[str, ...] = (
    "什么是",
    "为什么",
    "怎么",
    "如何",
    "哪些",
    "是否",
    "介绍",
    "解释",
    "说说",
    "讲讲",
    "概括",
    "分析",
    "评价",
    "阅读",
    "告诉我",
    "报告说了",
    "报告讲了",
)


def _has_output_action_before_noun(text: str) -> bool:
    """只接受位于产物名词之前的动作，避开“报告写得/做得很好”。"""

    noun_positions = [text.find(noun) for noun in _OUTPUT_NOUNS if text.find(noun) >= 0]
    for action in _OUTPUT_ACTIONS:
        action_pos = text.find(action)
        if action_pos < 0:
            continue
        suffix = text[action_pos + len(action) :]
        if suffix.startswith(("得", "的", "过", "完", "好", "了")):
            continue
        if any(action_pos <= noun_pos <= action_pos + 96 for noun_pos in noun_positions):
            return True
    return False


def _requested_artifact_kind(text: str) -> IntentKind | None:  # noqa: PLR0911, PLR0912
    """对明确产出型请求做确定性路由。

    调研/报告/方案默认产出 Markdown；用户明示指定 PPT/Word/PDF/HTML/
    Excel/TXT 时保留指定格式。普通问句不因出现“报告/文档”而触发产出。
    """

    lower = text.strip().lower()
    if not lower:
        return None
    has_noun = any(noun in lower for noun in _OUTPUT_NOUNS)
    if not has_noun:
        return None

    request_positions = [
        lower.find(marker) for marker in _OUTPUT_REQUEST_MARKERS if lower.find(marker) >= 0
    ]
    noun_positions = [lower.find(noun) for noun in _OUTPUT_NOUNS if lower.find(noun) >= 0]
    request_targets_output = any(
        request_pos <= noun_pos <= request_pos + 18
        for request_pos in request_positions
        for noun_pos in noun_positions
    )
    has_action = _has_output_action_before_noun(lower)
    command_text = lower.lstrip("@ ")
    for prefix in ("现在", "马上", "立即"):
        if command_text.startswith(prefix):
            command_text = command_text[len(prefix) :].lstrip("，, ")
            break
    action_is_command = has_action and (
        bool(request_positions) or command_text.startswith(_OUTPUT_ACTIONS)
    )
    looks_like_question = any(mark in lower for mark in ("?", "？")) or any(
        marker in lower for marker in _QUESTION_ONLY_MARKERS
    )
    if looks_like_question and not action_is_command:
        return None

    direct_research_command = lower.lstrip("@ ").startswith(("调研", "深度研究"))
    exact_output_noun = lower.lstrip("@ ") in _OUTPUT_NOUNS
    if not (
        request_targets_output or action_is_command or direct_research_command or exact_output_noun
    ):
        return None

    if any(token in lower for token in ("ppt", "幻灯")):
        return "generate_pptx"
    if any(token in lower for token in ("excel", "xlsx", "表格", "财务模型")):
        return "generate_xlsx"
    if any(token in lower for token in ("word", "docx")):
        return "generate_word"
    if "pdf" in lower:
        return "generate_pdf"
    if any(token in lower for token in ("html", "网页", "页面")):
        return "generate_html"
    if any(token in lower for token in ("txt", "纯文本", "文本文件")):
        return "generate_txt"
    if "文档" in lower and not any(token in lower for token in ("调研", "研究", "报告", "方案")):
        return "generate_word"
    return "generate_markdown"


def is_ppt_generation_request(text: str) -> bool:
    """Detect a PPT creation request without treating every mention as generation.

    This deterministic check only opens the main-model planning gate.  It never
    selects a template or authorizes artifact generation.
    """

    lower = text.strip().lower()
    ppt_positions = [lower.find(token) for token in ("ppt", "幻灯片", "幻灯")]
    ppt_positions = [position for position in ppt_positions if position >= 0]
    if not ppt_positions:
        return False
    command = lower.lstrip("@ ")
    if command.startswith(("生成 ppt", "生成ppt", "做 ppt", "做ppt", "幻灯片", "幻灯")):
        return True
    first_ppt = min(ppt_positions)
    prefix = lower[:first_ppt]
    return any(action in prefix for action in _OUTPUT_ACTIONS)


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
    """Return a non-authoritative candidate hint for the main-model planner.

    优先级（从高到低）：
      1. ``@chat`` 显式逃生 → ``chat_no_rag``（用户明确不要 RAG）
      2. 明确产出请求（调研/报告/方案）→ ``generate_*``
      3. 强 RAG 信号词组（"基于附件" / "产品手册里" / ...）→ ``search_rag``
      4. 现有 _KEYWORD_HINTS 关键字（生成产物 / web 搜索 / 纪要 / 等）
      5. 问句信号（含问号 / 含 "什么 / 介绍" 等）→ ``search_rag``（默认走 RAG）
    """
    # 任何调用方都不得用返回值直接派发；它只能进入 planning prompt。
    # 1. chat_no_rag 显式 escape
    if _chat_no_rag_explicit(text):
        return "chat_no_rag", 0.95

    # 2. 产出语义高于“基于附件”：用户要的是文件，不是内联问答。
    output_kind = _requested_artifact_kind(text)
    if output_kind is not None:
        return output_kind, 0.98

    # 3. 强 RAG 信号 → 0.9 高置信度
    if _strong_rag_hit(text):
        return "search_rag", 0.9

    # 4. 现有 keyword token 匹配（小写）
    lower = text.lower()
    for kw, kind in _KEYWORD_HINTS.items():
        if kw in lower:
            return kind, 0.85

    # 5. 问句信号兜底（默认走 RAG）
    if _is_question(text):
        return "search_rag", 0.7

    return None

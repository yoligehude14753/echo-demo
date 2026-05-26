"""Intent schema：@路由 9 类意图。

PR-16 / m5-t5：用户在前端聊天框输入"@查英伟达营收"等，
调 /intent/route → LLM 分类返回 IntentResult{kind, params, confidence}
→ 前端按 kind 触发对应业务（产物/搜索/会议）。

9 类意图：
- search_web        : @查 / @搜 / @最新（联网检索）
- search_rag        : @回忆 / @上次会议 / @找文档（本地知识库检索）
- generate_html     : @生成 HTML / @报告
- generate_pptx     : @生成 PPT / @幻灯片
- generate_xlsx     : @生成 Excel / @表格 / @财务模型
- generate_word     : @生成 Word / @文档
- summarize_meeting : @总结当前会议 / @生成纪要
- start_meeting     : @开始会议 / @新建会议
- chat              : 兜底，不带 @ 或不匹配上述意图
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

IntentKind = Literal[
    "search_web",
    "search_rag",
    "generate_html",
    "generate_pptx",
    "generate_xlsx",
    "generate_word",
    "summarize_meeting",
    "start_meeting",
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
        "summarize_meeting",
        "start_meeting",
        "chat",
    ]
)


class IntentResult(BaseModel):
    kind: IntentKind
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class IntentRequest(BaseModel):
    text: str
    current_meeting_id: str | None = None  # 提供给 summarize_meeting / start_meeting 用


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
_KEYWORD_HINTS: dict[str, IntentKind] = {
    "查": "search_web",
    "搜": "search_web",
    "最新": "search_web",
    "新闻": "search_web",
    "回忆": "search_rag",
    "上次": "search_rag",
    "之前": "search_rag",
    "找文档": "search_rag",
    "找资料": "search_rag",
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
    "开始会议": "start_meeting",
    "新建会议": "start_meeting",
}


def keyword_route(text: str) -> tuple[IntentKind, float] | None:
    """关键字快速路由：命中明确 token 返回高置信度，避免每次调 LLM。"""
    lower = text.lower()
    for kw, kind in _KEYWORD_HINTS.items():
        if kw in lower:
            return kind, 0.85
    return None

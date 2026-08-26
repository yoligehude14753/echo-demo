"""
Memory Extractor — LLM-driven extraction of nodes and edges from conversation.

Extraction is instruction-driven (no subjective scoring).
The LLM identifies entities and relationships; heat/importance is determined
purely by subsequent objective signals (access frequency, user instructions).

A lightweight heuristic pleasantry filter runs first: if the entire exchange
contains only social pleasantries (greetings, thanks, short filler), the LLM
call is skipped entirely — keeps the graph clean and saves tokens.

Output schema:
{
  "nodes": [
    {"name": "...", "dimension": "person|event|logic|knowledge",
     "description": "...", "aliases": [...]}
  ],
  "edges": [
    {"from": "node name", "to": "node name", "relation": "..."}
  ]
}
"""
from __future__ import annotations

import json
import re

from loguru import logger
from yoli_llm import call_llm

from app.config import get_config

# ── Pleasantry filter ─────────────────────────────────────────────────────────

_PLEASANTRY_PATTERNS = re.compile(
    r"^(好的?|嗯+|谢谢|不客气|对|好好|好的好的|哦|啊|哈哈+|嗯嗯+|"
    r"你好|再见|拜拜|好吧|行|好哒|好嗒|ok|okay|yeah|yes|no|thanks|"
    r"好棒|太好了|真的吗|明白了|了解|知道了|收到|好的收到|没问题|"
    r"辛苦了|加油|继续|继续呀|说下去)\W*$",
    re.IGNORECASE,
)

_MIN_CONTENT_CHARS = 20  # 更短的内容通常无实质信息

# ── 广播/媒体内容检测 ──────────────────────────────────────────────────────────
# 这些模式出现时，大概率是电视/广播/视频的背景声，不属于用户自己的信息
_BROADCAST_PATTERNS = re.compile(
    r"(ming pao|明报|本台|本站|本频道|点赞|订阅|关注|转发|直播|播报|"
    r"新闻联播|主持人|记者|采访|敬请期待|收看|收听|频道|栏目|"
    r"本节目|节目组|版权所有|保留版权|如有雷同|广告|赞助商|"
    r"thank you for watching|subscribe|click the bell|"
    r"stay tuned|back after the break|commercial break)",
    re.IGNORECASE,
)

# 用户自身发言的一人称信号（有这些才算"用户的话"）
_FIRST_PERSON_PATTERNS = re.compile(
    r"(我|我们|我的|我想|我要|我需要|我觉得|我认为|我喜欢|我不|"
    r"我有|我去|我在|我做|我说|我看|我听|我买|我用|你好|Echo|嗨)",
    re.IGNORECASE,
)


def _is_broadcast_content(turns: list[dict]) -> bool:
    """检测是否为电视/广播/视频背景声，若是则跳过记忆抽取。"""
    user_text = " ".join(
        t["content"] for t in turns if t.get("role") == "user"
    )
    # 命中广播关键词
    if _BROADCAST_PATTERNS.search(user_text):
        return True
    # 内容足够长但没有任何一人称信号 → 可能是旁观/背景内容
    if len(user_text) > 30 and not _FIRST_PERSON_PATTERNS.search(user_text):
        return True
    return False


def _is_pleasantry_only(turns: list[dict]) -> bool:
    """
    Heuristic: return True if every user turn is a short pleasantry.
    Skips LLM extraction call when True.
    """
    user_turns = [t["content"].strip() for t in turns if t.get("role") == "user"]
    if not user_turns:
        return True

    substantive = [
        t for t in user_turns
        if len(t) >= _MIN_CONTENT_CHARS or not _PLEASANTRY_PATTERNS.match(t)
    ]
    return len(substantive) == 0

# ── Ambient extraction prompt（保守：事实、知识、低置信度）─────────────────────

_EXTRACTION_PROMPT = """你是一个信息提取助手。从以下对话片段中提取结构化知识。

【维度说明】
- person：人物（用户提到的人，包括用户自己）
- event：事件（发生的事、计划、记忆）
- logic：逻辑规则（用户的偏好、习惯、规则）
- knowledge：知识（客观事实、用户学到的东西）

【输出要求】
只输出 JSON，不要解释。格式：
{{
  "nodes": [
    {{"name": "节点名称", "dimension": "person|event|logic|knowledge",
      "description": "简洁描述（一句话）", "aliases": []}}
  ],
  "edges": [
    {{"from": "节点A名称", "to": "节点B名称", "relation": "关系动词"}}
  ]
}}

如果没有值得提取的信息，返回 {{"nodes": [], "edges": []}}。

【对话片段】
{conversation}
"""

# ── Personal extraction prompt（激进：人际关系、情感语境、承诺计划）───────────

_PERSONAL_EXTRACTION_PROMPT = """你是 Echo 的记忆提取助手。以下是用户（Yoli）与其他人的真实对话记录。
这些内容来自用户亲身参与的对话，信息可靠性高。

【提取重点——比普通对话更细致】
1. person：出现的人物（谁在说话、被提及的人、关系）
2. event：计划、承诺、约定（"三点见"、"周五交报告"等有时间的事）
3. logic：用户对人/事的态度、偏好、评价
4. knowledge：对话中出现的新信息（地点、价格、结论等）

【特别关注】
- 说话双方的关系（朋友/同事/家人？语气线索）
- 情感信号（兴奋、担忧、无聊）
- 尚未完成的事项（"还没决定"、"等确认"）

【输出要求】
只输出 JSON，不要解释：
{{
  "nodes": [
    {{"name": "节点名称", "dimension": "person|event|logic|knowledge",
      "description": "具体描述（包含时间/人物/情境细节）", "aliases": []}}
  ],
  "edges": [
    {{"from": "节点A名称", "to": "节点B名称", "relation": "关系动词"}}
  ]
}}

对话中即使只有一句话也可能有价值，不要过度过滤。
如确实无有效信息，返回 {{"nodes": [], "edges": []}}。

【用户对话记录】
{conversation}
"""


def _parse_llm_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON from LLM output."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
    return json.loads(raw)


async def extract_from_conversation(
    turns: list[dict],  # [{"role": "user"|"assistant", "content": "..."}]
) -> dict:
    """
    Extract knowledge graph nodes and edges from ambient/dialogue turns.
    Applies pleasantry filter and broadcast filter.
    Returns {"nodes": [...], "edges": [...]}
    """
    if not turns:
        return {"nodes": [], "edges": []}

    if _is_pleasantry_only(turns):
        logger.debug("Memory extractor: skipped — pleasantry-only exchange")
        return {"nodes": [], "edges": []}

    if _is_broadcast_content(turns):
        logger.info("Memory extractor: skipped — broadcast/background content detected")
        return {"nodes": [], "edges": []}

    conv_text = "\n".join(
        f"{'用户' if t['role'] == 'user' else 'Echo'}: {t['content']}"
        for t in turns[-10:]
    )
    prompt = _EXTRACTION_PROMPT.format(conversation=conv_text)

    try:
        cfg = get_config()
        raw = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=cfg.LLM_NANO_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = _parse_llm_json(raw)
        nodes, edges = result.get("nodes", []), result.get("edges", [])
        logger.debug(f"[Extractor] ambient: {len(nodes)} nodes, {len(edges)} edges")
        return {"nodes": nodes, "edges": edges}
    except json.JSONDecodeError as e:
        logger.warning(f"Memory extractor: JSON parse failed — {e}")
        return {"nodes": [], "edges": []}
    except Exception as e:
        logger.warning(f"Memory extractor error: {e}")
        return {"nodes": [], "edges": []}


async def extract_from_personal_conversation(
    turns: list[dict],
    speaker_info: str | None = None,
) -> dict:
    """
    Dedicated high-fidelity extractor for personal conversations (router_action='personal').

    Unlike extract_from_conversation:
    - Uses the personal prompt which emphasizes interpersonal context, plans,
      emotional signals, and relationship inference.
    - Does NOT apply broadcast filter (personal conversations are genuine).
    - Pleasantry filter still applies (pure social niceties have no memory value).
    - Returns {"nodes": [...], "edges": [...]}
    """
    if not turns:
        return {"nodes": [], "edges": []}

    if _is_pleasantry_only(turns):
        logger.debug("[Extractor] personal: skipped — pleasantry-only")
        return {"nodes": [], "edges": []}

    conv_text = "\n".join(
        f"{'用户' if t['role'] == 'user' else '对方'}: {t['content']}"
        for t in turns[-12:]  # personal conversations get more context window
    )
    if speaker_info:
        conv_text = f"[说话者: {speaker_info}]\n{conv_text}"

    prompt = _PERSONAL_EXTRACTION_PROMPT.format(conversation=conv_text)

    try:
        cfg = get_config()
        raw = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=cfg.LLM_NANO_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = _parse_llm_json(raw)
        nodes, edges = result.get("nodes", []), result.get("edges", [])
        logger.debug(f"[Extractor] personal: {len(nodes)} nodes, {len(edges)} edges")
        return {"nodes": nodes, "edges": edges}
    except json.JSONDecodeError as e:
        logger.warning(f"[Extractor] personal JSON parse failed — {e}")
        return {"nodes": [], "edges": []}
    except Exception as e:
        logger.warning(f"[Extractor] personal error: {e}")
        return {"nodes": [], "edges": []}


async def save_extraction(
    extracted: dict,
    session_id: str,
    graph,  # MemoryGraph instance
    source_confidence: float = 0.7,
) -> None:
    """
    Persist extracted nodes and edges into the memory graph.
    source_confidence flows into each node and affects retrieval ranking:
      personal=0.9, activate=0.8, ambient=0.4
    """
    nodes = extracted.get("nodes", [])
    edges = extracted.get("edges", [])

    name_to_id: dict[str, str] = {}

    for node in nodes:
        name = node.get("name", "").strip()
        dimension = node.get("dimension", "knowledge")
        description = node.get("description", "")
        aliases = node.get("aliases", [])

        if not name or dimension not in ("person", "event", "logic", "knowledge"):
            continue

        node_id = await graph.upsert_node(
            name=name,
            dimension=dimension,
            description=description,
            aliases=aliases,
            session_id=session_id,
            source_confidence=source_confidence,
        )
        name_to_id[name] = node_id

    for edge in edges:
        from_name = edge.get("from", "")
        to_name = edge.get("to", "")
        relation = edge.get("relation", "related_to")

        from_id = name_to_id.get(from_name)
        to_id = name_to_id.get(to_name)
        if not from_id or not to_id:
            continue

        await graph.upsert_edge(
            from_id=from_id,
            to_id=to_id,
            relation=relation,
            session_id=session_id,
        )

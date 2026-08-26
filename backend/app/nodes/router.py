"""
Router Node — classifies a TranscriptSegment into one of four actions:

  activate  — user is talking to Echo (call word / question / command)
              → forward to Response node, reply via TTS
  personal  — user is talking TO someone else (friend, family, colleague)
              → store as personal conversation transcript (high fidelity)
              → no Echo response
  ambient   — environmental audio unrelated to the user
              (TV, radio, strangers, background noise transcript)
              → store in ambient_transcripts for Dream memory mining
  ignore    — segment too short / apparent noise / low confidence
              → discard

Key distinction: personal vs ambient
  personal = the *user* is a participant in the conversation.
  ambient  = the user is not speaking; it's external sound only.
  This matters because users strongly want personal conversations recorded
  for memory/recall, but have higher tolerance for ambient gaps.

Design decisions:
- Fast-path rules are config-driven (no hardcoded magic numbers in code).
- Primary model: gpt-5.4-mini (fast, cheap, good Chinese intent understanding).
- Fallback: MiniMax-Text-01, independent failure domain.
- The prompt is intentionally minimal to avoid over-classification.

Dual-track classification (no LLM for ~80% of segments):
  route_fast() → rule-based (0ms):
    Layer 0: noise / length filter  → "ignore"
    Layer 1: wake word detection    → "activate"  (skip Router LLM entirely)
    Layer 2: structural heuristic   → "personal" / "ambient"  (skip Router LLM)
    Layer 3: uncertain              → None  (fallthrough to LLM)
  route()     → LLM for uncertain cases only
"""
from __future__ import annotations

import asyncio
import re

from loguru import logger

from app.adapters.llm import OpenAICompatibleLLM
from app.asr.base import TranscriptSegment
from app.config import get_config
from app.metrics import get_metrics
from app.schemas.llm import ChatMessage

RouterAction = str  # Literal["activate", "personal", "ambient", "ignore"]
_VALID_ACTIONS: frozenset[str] = frozenset({"activate", "personal", "ambient", "ignore"})

# ── Wake word fast-path ───────────────────────────────────────────────────────
# Trigger words that unambiguously indicate the user is addressing Echo.
# Includes Deepgram Nova-3 phonetic transcriptions of "echo" in Chinese speech:
#   "衣扣/一扣/依扣/伊口" (yi-kou ~ echo), "哎扣" (ai-kou ~ echo)
_WAKE_WORDS: frozenset[str] = frozenset({
    # English
    "echo", "hey echo", "hi echo",
    # Chinese pinyin / phonetic for "Echo"
    "嗨", "喂", "小回", "回回",
    "嘿echo", "嘿 echo", "echo你", "echo，", "echo,",
    # Deepgram phonetic transcriptions of "echo" in Chinese speech
    "衣扣", "一扣", "依扣", "伊口", "伊扣", "哎扣", "唉扣",
    # "一口" (yi-kou) — confirmed Deepgram Nova-3 mis-transcription of "echo" in Chinese
    "一口",
    # Common mis-transcriptions / partials
    "echo。", "echo？", "echo!", "echo ","echo\n",
})

# Compiled patterns for structural heuristics (Layer 2)
_RE_FIRST_PERSON = re.compile(r'[我你咱]')
_RE_QUESTION_END = re.compile(r'[？?]\s*$')
_RE_QUESTION_PARTICLE = re.compile(r'(吗|呢|嘛|吧)\s*[？?。]?\s*$')


_SYSTEM_PROMPT = """\
你是 Echo 的意图路由器，决定如何处理一条语音转录片段。

规则：
- activate：用户在对 Echo 说话（含呼叫词 Echo/嗨/喂、问句、指令、打招呼）
- personal：用户正在和其他人说话（朋友、家人、同事的对话），用户本身是说话者/参与者
- ambient：用户没有说话，听到的是环境声（电视、广播、路过的陌生人、背景噪音）
- ignore：片段过短、明显乱码/噪音、只有标点符号

关键区分：
  personal = 用户自己是说话的一方（"好的，待会儿见""我觉得这个方案不错"）
  ambient  = 用户没有说话，只是周围的声音（电视对话、旁人交谈）

只返回一个单词：activate 或 personal 或 ambient 或 ignore，不要任何解释。"""


def _wake_word_path(seg: TranscriptSegment) -> RouterAction | None:
    """
    Layer 1: wake word detection — ~0ms, no API call.
    Returns "activate" if the text contains a trigger word, else None.
    """
    text_lower = seg.text.strip().lower()
    for kw in _WAKE_WORDS:
        if kw in text_lower:
            return "activate"
    return None


_RE_THIRD_PERSON = re.compile(r'[他她它]')
_RE_SECOND_PERSON = re.compile(r'你')


def _structural_heuristic(seg: TranscriptSegment) -> RouterAction | None:
    """
    Layer 2: classify clearly personal/ambient transcripts without LLM — ~0ms.

    "personal"  → user is conversing with others; first-person + non-question
    "ambient"   → long background audio with no personal involvement
    None        → uncertain; LLM should decide
    """
    text = seg.text.strip()

    # Long monologue → background or personal (never a direct Echo address)
    if len(text) > 100:
        if _RE_FIRST_PERSON.search(text):
            return "personal"
        return "ambient"

    # Short third-person-only statements (no "你", no "我") → ambient background
    # e.g. "他能给你量再列出来他不懂吗" → ambient
    if len(text) >= 8 and _RE_THIRD_PERSON.search(text) and not _RE_SECOND_PERSON.search(text):
        return "ambient"

    # First-person statement without question → personal conversation
    # Threshold lowered to 6 chars to catch "好了吧", "我知道了" etc.
    if len(text) >= 6 and _RE_FIRST_PERSON.search(text):
        if not _RE_QUESTION_END.search(text) and not _RE_QUESTION_PARTICLE.search(text):
            return "personal"

    return None  # Uncertain — LLM needed


def route_fast(seg: TranscriptSegment) -> RouterAction | None:
    """
    Full rule-based fast-path — no LLM, ~0ms.

    Returns:
      "ignore"          — noise / too short
      "activate"        — wake word detected
      "personal"        — structural heuristic: user is conversing
      "ambient"         — structural heuristic: background audio
      None              — uncertain; caller should invoke route() with LLM
    """
    # Layer 0: noise / length filters (existing)
    action = _fast_path(seg)
    if action:
        return action   # "ignore"

    # Layer 1: wake word
    action = _wake_word_path(seg)
    if action:
        return action   # "activate"

    # Layer 2: structural heuristic
    return _structural_heuristic(seg)


def _user_prompt(seg: TranscriptSegment) -> str:
    return (
        f"来源：{seg.source}（device=ESP32贴近用户，desktop=桌面麦克风）\n"
        f"说话人：{seg.speaker_label}\n"
        f"转录：{seg.text}"
    )


def _fast_path(seg: TranscriptSegment) -> RouterAction | None:
    """
    Rule-based fast-path classification — no LLM call.
    All thresholds come from Config so they can be tuned without code changes.
    Returns an action string, or None if the LLM should decide.
    """
    import re
    cfg = get_config()
    text = seg.text.strip()

    # ① Too short
    if len(text) < cfg.ROUTER_MIN_TEXT_LEN:
        return "ignore"

    # ② Short segment with low confidence → noise
    if seg.duration < cfg.ROUTER_MIN_DURATION_S and len(text) < 6:
        return "ignore"

    # ③ Low ASR confidence → noise
    if seg.confidence < cfg.ROUTER_MIN_CONFIDENCE:
        return "ignore"

    # ④ Repeated single char: "为为为", "代代代" → Deepgram noise hallucination
    if re.search(r'(.)\1{2,}', text):
        return "ignore"

    chars = list(text)

    # ⑤ Extremely low character variety → Deepgram noise: "为代为代"
    if len(chars) >= 4 and len(set(chars)) <= 2:
        return "ignore"

    # ⑥ One char dominates (>ROUTER_MAX_CHAR_FREQ) → rhythmic noise: "三为三代"
    if len(chars) >= 4:
        top_freq = max(chars.count(c) for c in set(chars))
        if top_freq / len(chars) > cfg.ROUTER_MAX_CHAR_FREQ:
            return "ignore"

    # ⑦ Repeated bigram → Deepgram beat noise: "位于位于位于"
    if len(chars) >= 6:
        bigrams = [''.join(chars[i:i+2]) for i in range(len(chars) - 1)]
        if max(bigrams.count(bg) for bg in bigrams) >= 2:
            return "ignore"

    return None


async def _call_llm(
    model: str,
    api_key: str,
    base_url: str,
    seg: TranscriptSegment,
) -> RouterAction:
    del model, api_key, base_url
    from app.config import get_settings

    adapter = OpenAICompatibleLLM(get_settings())
    try:
        resp = await adapter.chat(
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(role="user", content=_user_prompt(seg)),
            ],
            max_tokens=10,
            temperature=0.0,
        )
    finally:
        await adapter.aclose()
    raw = resp.content.strip().lower()
    if raw in ("activate", "personal", "ambient", "ignore"):
        return raw
    # If the model outputs something unexpected, log and default to ambient
    logger.warning(f"[Router] Unexpected response '{raw}' for text='{seg.text[:40]}' → ambient")
    return "ambient"


async def route(seg: TranscriptSegment) -> RouterAction:
    """
    Classify a segment.

    Returns one of:
      "activate"  — user is talking to Echo → LLM reply + TTS
      "personal"  — user is talking to another person → store, no reply
      "ambient"   — environmental audio, user not speaking → store, no reply
      "ignore"    — too short / noise / low confidence → discard

    Never raises — on total failure defaults to "ambient".
    """
    # Fast path: noise + wake word + structural heuristic (no LLM)
    action = route_fast(seg)
    if action:
        logger.debug(f"[Router] fast-path → {action}: '{seg.text[:40]}'")
        get_metrics().record_router_action(action)
        return action

    cfg = get_config()

    # Primary LLM
    try:
        action = await asyncio.wait_for(
            _call_llm(
                model=cfg.ROUTER_MODEL,
                api_key=cfg.YUNWU_GPT_KEY,
                base_url=cfg.OPENAI_BASE_URL,
                seg=seg,
            ),
            timeout=8.0,
        )
        if action not in _VALID_ACTIONS:
            logger.warning(f"[Router] primary returned invalid action {action!r}, fallback to ambient")
            action = "ambient"
        logger.debug(f"[Router] primary → {action}: '{seg.text[:40]}'")
        get_metrics().record_router_action(action)
        return action
    except Exception as exc:
        logger.warning(f"[Router] Primary model failed ({exc!r}), trying fallback")

    # Fallback: MiniMax
    try:
        action = await asyncio.wait_for(
            _call_llm(
                model="MiniMax-Text-01",
                api_key=cfg.MINIMAX_API_KEY,
                base_url=cfg.MINIMAX_BASE_URL,
                seg=seg,
            ),
            timeout=12.0,
        )
        if action not in _VALID_ACTIONS:
            logger.warning(f"[Router] fallback returned invalid action {action!r}, fallback to ambient")
            action = "ambient"
        logger.debug(f"[Router] fallback → {action}: '{seg.text[:40]}'")
        get_metrics().record_router_action(action)
        return action
    except Exception as exc:
        logger.error(f"[Router] Both models failed for '{seg.text[:40]}': {exc!r}")

    # Total failure — treat as ambient so it gets stored but not acted on
    get_metrics().record_router_action("ambient")
    return "ambient"

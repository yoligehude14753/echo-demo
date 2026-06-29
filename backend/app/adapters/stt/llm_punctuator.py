"""STT 后处理：用 LLM_FAST 给 FireRed 输出补标点 + 分段。

为什么需要：
- FireRedASR2 的 OpenAPI schema 只接受 file / model / language /
  response_format / timestamp_granularities —— **没有 punctuation / vad / punc
  开关**。实测 6s ambient chunk 出来的中文是一气呵成 30+ 字、无标点的整行，
  用户反馈"读不下去"。
- 既然 STT 服务侧无法直出标点，就在 ambient 主链路里加一个轻量 LLM 后处理：
  LLM_FAST（默认跟随 MAIN；私有部署可切本地 vLLM），把多段 raw text **一次性**
  发给 LLM，要求"只加标点和换行，不改字、不补字、不总结"。

设计约束（与 18-llm-workflow.mdc / 19-quality-detail.mdc 对齐）：
- **结构化输入输出**：输入 JSON {"items": [{"id": int, "text": str}]}；
  输出 JSON {"items": [{"id": int, "text": str}]}。id 顺序必须一一对应。
- **fail-soft**：超时 / 解析失败 / id 不齐 → 退回原文本，**不**抛异常。
  ambient 主链路 99% 时间在跑，任何阻塞都会让 stored counter 卡住。
- **温度 0**：标点是确定性任务，避免 LLM 二次创作。
- **批 batch**：单 chunk 通常只有 1-3 段，一次 LLM 调用 < 300 tokens prompt +
  < 500 tokens completion，配 2s 超时；超时则回退原文，不阻塞转写主链路。
- **flag 可关**：`AMBIENT_LLM_PUNCTUATE=false` 整体禁用；测试 / 出问题时
  立刻回退到无标点路径。

边界保护：
- 输出文本如果长度异于原文 ±30% → 视为 LLM 自由发挥，丢弃改动。
  之前 echo 用过类似的 stt_llm_correct 加规则发现 LLM 偶尔会把"嗯嗯嗯"
  扩成完整句子，这条护栏拦得住。
- 标点字符集白名单：只允许 `，。！？、；：「」""''（）—— …` + 换行；
  其它非汉字/英数/空白 全部剥掉再校验。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.llm import ChatMessage
from app.schemas.meeting import TranscriptSegment

logger = logging.getLogger("echodesk.stt.punctuator")

# 标点字符集白名单（中文常见 + 西文兜底）；超出此集 → 视为 LLM 加字
_ALLOWED_PUNCT = set("，。！？、；：「」“”‘’（）()——…—.,!?;:\"'`<>《》【】[]{} \t\n\r")

# 输出文本相比原文长度允许变化范围（仅标点 + 换行不应大幅膨胀/收缩）。
# 短文本（< 20 字）按"绝对增量 ≤ 8 个标点"放宽，因为 1-2 个标点
# 对短文本的比例就 30%+，会被无意义拒绝。
_MAX_LEN_GROWTH_RATIO = 1.30
_MAX_LEN_GROWTH_ABS = 8
_MIN_LEN_SHRINK = 0.70

_PROMPT_SYS = (
    "你是中文标点修正器。\n"
    "唯一任务：给输入的中文转写文本补充自然标点（，。！？等）和必要的换行分段。\n"
    "硬约束（每条都不可违反）：\n"
    "1. 不修改任何汉字/英文字符/数字 —— 一个字符都不能增删替换。\n"
    "2. 不补全省略、不解释缩写、不展开口语 —— 哪怕原文不通顺也保持原样。\n"
    "3. 不做总结、不改写、不翻译、不评论。\n"
    "4. 只允许新增：，。！？、；：（）「」“”\n"
    '5. 输出必须是合法 JSON，schema：{"items": [{"id": <整数>, "text": <字符串>}]}。\n'
    "6. id 必须与输入一一对应、顺序不变；条数完全相同。\n"
    "7. 不输出任何 JSON 之外的解释 / 思考 / Markdown 包裹。"
)


@dataclass(frozen=True, slots=True)
class _BatchItem:
    idx: int
    original: str


class LLMPunctuator:
    """对 STT 输出的 TranscriptSegment 列表批量加标点。

    设计选择：take LLM port + Settings；不做循环（一次 chunk 通常 1-3 段，
    单 LLM 调用足够）。失败时降级返回原 segments，**绝不抛异常**。
    """

    def __init__(
        self,
        llm: LLMPort,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.ambient_llm_punctuate

    async def punctuate(
        self,
        segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """对一组 TranscriptSegment 加标点；失败降级返回原列表。

        注意：**不复用** speaker_id / start_ms / end_ms，只重写 `.text`。
        """
        if not self.enabled or not segments:
            return segments

        items = [
            _BatchItem(idx=i, original=seg.text.strip())
            for i, seg in enumerate(segments)
            if seg.text and seg.text.strip()
        ]
        if not items:
            return segments

        try:
            updates = await asyncio.wait_for(
                self._call_llm(items),
                timeout=self._settings.ambient_punctuator_timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "ambient punctuator timeout (>%.1fs), falling back to raw text",
                self._settings.ambient_punctuator_timeout_s,
            )
            return segments
        except Exception as e:  # pragma: no cover - LLM 底层异常分支
            logger.warning("ambient punctuator failed: %s; falling back", e)
            return segments

        if not updates:
            return segments

        out: list[TranscriptSegment] = []
        for i, seg in enumerate(segments):
            new_text = updates.get(i)
            if new_text is None:
                out.append(seg)
            else:
                out.append(seg.model_copy(update={"text": new_text}))
        return out

    async def _call_llm(self, items: list[_BatchItem]) -> dict[int, str]:
        """发送一次 LLM 请求，返回 idx → 新文本（仅对通过校验的项）。"""
        payload = {
            "items": [{"id": it.idx, "text": it.original} for it in items],
        }
        user_msg = "请按 system 约束给以下段落加标点。\n输入：\n" + json.dumps(
            payload, ensure_ascii=False
        )
        resp = await self._llm.chat(
            [
                ChatMessage(role="system", content=_PROMPT_SYS),
                ChatMessage(role="user", content=user_msg),
            ],
            model=self._settings.llm_fast_model,
            max_tokens=self._settings.llm_fast_max_tokens,
            temperature=0.0,
            timeout_s=self._settings.ambient_punctuator_timeout_s,
        )
        content = (resp.content or "").strip()
        if not content:
            return {}

        parsed = _safe_parse_items(content)
        if parsed is None:
            logger.debug("punctuator: cannot parse LLM JSON; raw=%r", content[:200])
            return {}

        original_by_idx = {it.idx: it.original for it in items}
        result: dict[int, str] = {}
        for item in parsed:
            idx = item.get("id")
            new_text = item.get("text")
            if not isinstance(idx, int) or not isinstance(new_text, str):
                continue
            original = original_by_idx.get(idx)
            if original is None:
                continue
            cleaned = new_text.strip()
            if not _is_safe_rewrite(original, cleaned):
                logger.debug(
                    "punctuator: reject unsafe rewrite idx=%d orig=%r new=%r",
                    idx,
                    original[:60],
                    cleaned[:60],
                )
                continue
            result[idx] = cleaned
        return result


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _safe_parse_items(content: str) -> list[dict[str, object]] | None:
    """从 LLM 输出里抽出 JSON object，提取 items list；失败返回 None。

    宽容处理：
    - 模型偶尔会被 markdown ```json ... ``` 包裹 → 抽中间的对象
    - 模型偶尔会在 JSON 之前/之后塞额外说明 → 取第一个 ``{`` 到最后一个 ``}``
    """
    candidate = content
    if "```" in candidate:
        # 把 ``` 块剔掉、保留最长的 ```json 块内容（取第一段）
        parts = candidate.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                candidate = stripped
                break

    if not (candidate.startswith("{") and candidate.endswith("}")):
        match = _JSON_OBJ_RE.search(candidate)
        if not match:
            return None
        candidate = match.group(0)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def _strip_for_compare(text: str) -> str:
    """剥掉所有标点 + 空白，只保留汉字/英数 用于"字符内容不变"比对。"""
    return "".join(ch for ch in text if ch not in _ALLOWED_PUNCT)


def _is_safe_rewrite(original: str, rewritten: str) -> bool:
    """校验 LLM 输出是不是"只动了标点、没动文字"。

    五条护栏：
    1. 长度增长 ≤ 30%（标点 + 换行不会让文本翻倍）
    2. 长度收缩 ≥ 70%（不许大幅删字）
    3. 剥掉所有标点 + 空白后，原文 == 改写（**核心**：字符级一致）
    4. 改写文本不含非白名单字符（防 LLM 注入 emoji / 控制符）
    5. 不能是空字符串
    """
    if not rewritten:
        return False
    orig_len = max(len(original), 1)
    new_len = len(rewritten)
    max_allowed = max(orig_len * _MAX_LEN_GROWTH_RATIO, orig_len + _MAX_LEN_GROWTH_ABS)
    if new_len > max_allowed:
        return False
    if new_len < orig_len * _MIN_LEN_SHRINK:
        return False
    # 内容字符（剥标点）必须严格一致
    if _strip_for_compare(original) != _strip_for_compare(rewritten):
        return False
    # 不允许出现非白名单的奇怪字符（emoji / 控制字符）
    for ch in rewritten:
        if ch in _ALLOWED_PUNCT:
            continue
        # 汉字 / 英数 / 下划线 / "·" 都视作内容字符
        if (
            "\u4e00" <= ch <= "\u9fff"  # CJK 常用
            or "\u3400" <= ch <= "\u4dbf"  # CJK Ext A
            or ch.isalnum()
            or ch in "_-·"
        ):
            continue
        return False
    return True


__all__ = ["LLMPunctuator"]

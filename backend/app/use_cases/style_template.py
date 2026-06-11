"""use_case: 从知识库自动发现"样式模板"并抽离样式注入文档生成。

用户原话（2026-06-05）："模板就是主动发现本地的文档，就是读知识库里的文件，
也接受用户主动扔文件进入 echo，或者在知识库里检索并匹配"。

做法：生成 Word（公文/通用文档）时，用 brief 在 RAG 知识库检索最相关的本地 .docx，
取其原始文件路径（chunk metadata 的 ``source_path``），用 ``docx_style`` 抽出版式
规格，转成中文样式指令追加到 ``extra_instructions``。这样生成的新文档自动复刻知识库
里同类参考件的字体/边距/标题样式，无需用户显式上传或指定模板。

仅依赖 RagPort + python-docx；找不到合适模板就返回 None，不影响正常生成。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.ports.rag import RagPort
from app.use_cases.docx_style import extract_style_instructions

logger = logging.getLogger("echodesk.style_template")

_QUERY_TOP_K = 20
# 候选 doc 的聚合相关度需达到此阈值才采用（避免无关 .docx 被硬拉来当模板）。
_MIN_AGG_SCORE = 0.5


@dataclass(slots=True)
class StyleTemplate:
    source_path: str
    title: str
    instructions: str


def _best_existing_docx(chunks: list, suffix: str = ".docx") -> Path | None:
    """聚合命中后缀的 source_path 相关度，返回相关度最高且磁盘存在的文件路径。"""
    agg: dict[str, float] = defaultdict(float)
    for c in chunks:
        sp = c.metadata.get("source_path")
        if not sp or not str(sp).lower().endswith(suffix):
            continue
        agg[str(sp)] += float(getattr(c, "score", 0.0) or 0.0)
    if not agg:
        return None
    best_path, best_score = max(agg.items(), key=lambda kv: kv[1])
    if best_score < _MIN_AGG_SCORE:
        return None
    p = Path(best_path).expanduser()
    if not p.exists():
        logger.info("style template path missing on disk: %s", best_path)
        return None
    return p


async def resolve_docx_style_template(rag: RagPort, brief: str) -> StyleTemplate | None:
    """在知识库检索与 brief 最相关的本地 .docx，抽出可注入的样式指令。

    返回 None 表示：知识库没有 .docx、文件不存在、相关度不足、或抽离失败——
    任一情况都让上层走默认生成，不报错。
    """
    q = brief.strip()
    if not q:
        return None
    try:
        chunks = await rag.query(q, top_k=_QUERY_TOP_K)
    except Exception as e:
        logger.warning("style template query failed: %s", e)
        return None

    p = _best_existing_docx(chunks)
    if p is None:
        return None
    try:
        instructions = extract_style_instructions(str(p))
    except Exception as e:
        logger.warning("style extraction failed for %s: %s", p, e)
        return None
    return StyleTemplate(source_path=str(p), title=p.name, instructions=instructions)


def merge_extra_instructions(extra: str | None, style_instructions: str) -> str:
    """把样式指令并入 extra_instructions（样式指令置后，作为强约束补充）。"""
    if extra and extra.strip():
        return f"{extra.strip()}\n\n{style_instructions}"
    return style_instructions

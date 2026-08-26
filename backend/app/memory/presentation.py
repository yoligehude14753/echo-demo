"""Stable client projection for memory recall sources."""

from __future__ import annotations

from app.memory.models import RecallResult


def _source_title(level: str, kind: str, metadata: dict[str, object]) -> str:
    for key in ("title", "meeting_title", "artifact_name", "name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    level_names = {
        "L0": "当前对话",
        "L1": "历史会议与产物",
        "L2": "长期记忆",
        "L3": "个人配置",
    }
    kind_names = {
        "meeting_segment": "会议原文",
        "meeting_minutes": "会议纪要",
        "ambient_segment": "环境记录",
        "artifact": "工作产物",
        "fact": "事实记忆",
        "preference": "偏好记忆",
        "decision": "决策记忆",
        "todo": "待办记忆",
        "relationship": "人物关系",
    }
    return kind_names.get(kind) or level_names.get(level, "相关信息")


def recall_sources(result: RecallResult) -> list[dict[str, object]]:
    """Project owner-scoped recall matches into the desktop memory-card shape."""

    sources: list[dict[str, object]] = []
    for index, match in enumerate(result.matches, start=1):
        candidate = match.candidate
        metadata = dict(candidate.metadata)
        sources.append(
            {
                "index": index,
                "candidate_id": candidate.candidate_id,
                "memory_id": candidate.memory_id,
                "level": candidate.level,
                "kind": candidate.kind,
                "title": _source_title(candidate.level, candidate.kind, metadata),
                "excerpt": candidate.content[:1_500],
                "source_ref": candidate.source_ref,
                "occurred_at": candidate.occurred_at.isoformat(),
                "confidence": candidate.confidence,
                "relevance": match.relevance,
                "score": match.score,
                "relation": match.relation,
                "manageable": candidate.level in {"L2", "L3"},
                "metadata": metadata,
            }
        )
    return sources


__all__ = ["recall_sources"]

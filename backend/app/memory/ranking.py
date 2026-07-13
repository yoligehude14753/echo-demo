"""Deterministic pre-ranking for bounded multi-layer memory recall."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime

from app.memory.models import RecallCandidate
from app.memory.repository import normalize_text


def lexical_relevance(query: str, content: str) -> float:
    q = normalize_text(query).replace(" ", "")
    c = normalize_text(content).replace(" ", "")
    if not q or not c:
        return 0.0
    if q in c or c in q:
        coverage = min(len(q), len(c)) / max(len(q), len(c))
        return min(1.0, 0.72 + coverage * 0.28)
    q_units = set(q if len(q) < 3 else (q[i : i + 2] for i in range(len(q) - 1)))
    c_units = set(c if len(c) < 3 else (c[i : i + 2] for i in range(len(c) - 1)))
    if not q_units or not c_units:
        return 0.0
    return len(q_units & c_units) / math.sqrt(len(q_units) * len(c_units))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recency(candidate: RecallCandidate, now: datetime) -> float:
    half_life_days = {"L0": 0.25, "L1": 30.0, "L2": 120.0, "L3": 365.0}
    age_days = max(0.0, (now - _as_utc(candidate.occurred_at)).total_seconds() / 86_400)
    return math.exp(-math.log(2) * age_days / half_life_days[candidate.level])


def score_candidates(
    query: str,
    candidates: list[RecallCandidate],
) -> list[RecallCandidate]:
    now = datetime.now(UTC)
    scored: list[RecallCandidate] = []
    for item in candidates:
        relevance = lexical_relevance(query, item.content)
        recency = _recency(item, now)
        item.deterministic_score = (
            0.58 * relevance + 0.20 * recency + 0.14 * item.salience + 0.08 * item.confidence
        )
        scored.append(item)
    return sorted(scored, key=lambda item: item.deterministic_score, reverse=True)


def prefilter_candidates(
    query: str,
    candidates: list[RecallCandidate],
    *,
    limit: int,
    minimum_per_level: int = 4,
) -> list[RecallCandidate]:
    """Keep the global leaders plus a bounded representation of every layer."""

    ranked = score_candidates(query, candidates)
    selected: dict[str, RecallCandidate] = {
        item.candidate_id: item for item in ranked[: max(1, limit // 2)]
    }
    by_level: dict[str, list[RecallCandidate]] = defaultdict(list)
    for item in ranked:
        by_level[item.level].append(item)
    for level in ("L0", "L1", "L2", "L3"):
        for item in by_level[level][:minimum_per_level]:
            selected.setdefault(item.candidate_id, item)
    for item in ranked:
        if len(selected) >= limit:
            break
        selected.setdefault(item.candidate_id, item)
    return sorted(
        selected.values(),
        key=lambda item: item.deterministic_score,
        reverse=True,
    )[:limit]


__all__ = ["lexical_relevance", "prefilter_candidates", "score_candidates"]

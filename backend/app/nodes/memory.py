"""
Memory Node — persists every finalized TranscriptSegment to ambient_transcripts
and optionally resolves Deepgram's temporary SPEAKER_X labels to stable UUIDs
via resemblyzer (SpeakerResolver).

Design:
- All segments are stored regardless of router_action; Dream will mine them.
- priority encodes source quality:
    personal=2 > activate-derived=1 > ambient=0
  Dream processes higher-priority rows first, ensuring personal conversations
  are fully mined before ambient background noise.
- source_confidence flows from router_action into memory_nodes via extractor:
    personal=0.9, activate=0.8, ambient=0.4
  This makes source quality a persistent, query-time signal.
- SpeakerResolver runs only when audio bytes are available (for resemblyzer)
  or when an existing Deepgram label→UUID mapping already exists in the cache.
- A lightweight in-memory LRU cache reduces DB round-trips for repeated labels.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from app.asr.base import TranscriptSegment
from app.db import get_db


# ── Speaker label → UUID in-memory cache ─────────────────────────────────────
# key: "{device_id}:{speaker_label}"  value: speaker_uuid
_LABEL_CACHE_MAX = 64
_label_cache: OrderedDict[str, str] = OrderedDict()


def _cache_get(device_id: str, label: str) -> str | None:
    key = f"{device_id}:{label}"
    if key in _label_cache:
        _label_cache.move_to_end(key)
        return _label_cache[key]
    return None


def _cache_set(device_id: str, label: str, uuid: str) -> None:
    key = f"{device_id}:{label}"
    _label_cache[key] = uuid
    _label_cache.move_to_end(key)
    if len(_label_cache) > _LABEL_CACHE_MAX:
        _label_cache.popitem(last=False)


# ── Speaker resolution ────────────────────────────────────────────────────────

async def resolve_speaker_uuid(
    device_id: str,
    speaker_label: str,
    audio_bytes: Optional[bytes] = None,
) -> str | None:
    """
    Map Deepgram's temporary SPEAKER_X label to a persistent UUID.

    Steps:
    1. Check in-memory cache.
    2. Look up existing mapping in DB (speaker_profiles table).
    3. If audio provided and resemblyzer available, run voice embedding.
    4. Return UUID or None if unresolvable.
    """
    # 1. Memory cache
    cached = _cache_get(device_id, speaker_label)
    if cached:
        return cached

    # 2. DB lookup: check if this label was previously mapped for this device
    try:
        async with get_db() as conn:
            row = await (await conn.execute(
                """SELECT speaker_id FROM speaker_profiles
                   WHERE device_id=? AND label=? LIMIT 1""",
                (device_id, speaker_label),
            )).fetchone()
        if row:
            uuid = row["speaker_id"]
            _cache_set(device_id, speaker_label, uuid)
            return uuid
    except Exception as exc:
        logger.warning(f"[Memory] DB speaker lookup failed: {exc}")

    # 3. Voice embedding via resemblyzer (CPU, runs in thread pool)
    if audio_bytes:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                _identify_sync,
                audio_bytes,
                device_id,
            )
            if result:
                uuid = result["speaker_id"]
                _cache_set(device_id, speaker_label, uuid)
                # Persist the Deepgram label → UUID mapping so future lookups hit DB
                try:
                    async with get_db() as conn:
                        await conn.execute(
                            """UPDATE speaker_profiles SET label=?
                               WHERE speaker_id=? AND device_id=?""",
                            (speaker_label, uuid, device_id),
                        )
                        await conn.commit()
                except Exception:
                    pass
                return uuid
        except Exception as exc:
            logger.warning(f"[Memory] resemblyzer resolve failed: {exc}")

    return None


def _identify_sync(audio_bytes: bytes, device_id: str) -> dict | None:
    """Synchronous wrapper for speaker.diarizer.identify_speaker (runs in thread)."""
    try:
        from app.speaker.diarizer import identify_speaker
        return identify_speaker(audio_bytes, device_id)
    except Exception as exc:
        logger.debug(f"[Memory] resemblyzer not available: {exc}")
        return None


# ── Source quality mapping ────────────────────────────────────────────────────

# Priority: Dream processes personal first, then others chronologically.
_ACTION_PRIORITY: dict[str, int] = {
    "personal": 2,
    "activate": 1,  # activate turns go to sessions, but stored here if needed
    "ambient":  0,
    "ignore":   0,
}

# source_confidence: inherited by extracted memory_nodes.
# personal=user explicitly in conversation → high confidence facts
# ambient=overheard → lower confidence, may be misheard or irrelevant
SOURCE_CONFIDENCE: dict[str, float] = {
    "personal": 0.9,
    "activate": 0.8,
    "ambient":  0.4,
    "ignore":   0.3,
}


def action_to_priority(router_action: str | None) -> int:
    return _ACTION_PRIORITY.get(router_action or "ambient", 0)


def action_to_source_confidence(router_action: str | None) -> float:
    return SOURCE_CONFIDENCE.get(router_action or "ambient", 0.4)


# ── DB write ──────────────────────────────────────────────────────────────────

async def store_transcript(seg: TranscriptSegment) -> int:
    """
    Insert segment into ambient_transcripts with priority derived from router_action.
    priority=2 (personal) is processed before priority=0 (ambient) in Dream.
    Returns the inserted row id.
    """
    row = seg.to_db_row()
    priority = action_to_priority(seg.router_action)
    async with get_db() as conn:
        cursor = await conn.execute(
            """INSERT INTO ambient_transcripts
               (device_id, text, speaker_label, speaker_uuid, confidence,
                source, router_action, recorded_at, processed_for_memory, priority)
               VALUES
               (:device_id, :text, :speaker_label, :speaker_uuid, :confidence,
                :source, :router_action, :recorded_at, :processed_for_memory, :priority)""",
            {**row, "priority": priority},
        )
        await conn.commit()
        return cursor.lastrowid


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_ambient(
    seg: TranscriptSegment,
    audio_bytes: Optional[bytes] = None,
) -> int:
    """
    Full memory-node pipeline for a segment:
      1. Attempt speaker UUID resolution (cache → DB → resemblyzer).
      2. Store segment with resolved UUID.
      Returns DB row id.
    """
    # Resolve speaker if not already done
    if seg.speaker_uuid is None and seg.speaker_label:
        seg.speaker_uuid = await resolve_speaker_uuid(
            seg.device_id, seg.speaker_label, audio_bytes
        )

    row_id = await store_transcript(seg)
    logger.debug(
        f"[Memory] stored ambient_transcripts id={row_id} "
        f"speaker={seg.speaker_label}/{seg.speaker_uuid} "
        f"text='{seg.text[:40]}'"
    )
    return row_id


async def query_recent(
    device_id: str,
    limit: int = 50,
    source: str | None = None,
    priority_first: bool = True,
) -> list[dict]:
    """
    Fetch recent transcripts for a device.
    priority_first=True: personal (priority=2) surfaces before ambient for same timeframe.
    Used by retriever & debug APIs.
    """
    sql = """SELECT id, device_id, text, speaker_label, speaker_uuid, confidence,
                    source, router_action, recorded_at, priority
             FROM ambient_transcripts
             WHERE device_id=?"""
    params: list = [device_id]
    if source:
        sql += " AND source=?"
        params.append(source)
    if priority_first:
        sql += " ORDER BY priority DESC, recorded_at DESC LIMIT ?"
    else:
        sql += " ORDER BY recorded_at DESC LIMIT ?"
    params.append(limit)

    async with get_db() as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

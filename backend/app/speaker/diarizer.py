"""
声纹识别模块 — 基于 resemblyzer GE2E 模型。

功能：
- 每段音频提取 d-vector embedding (256维)
- 与已知声纹库比对（余弦相似度）
- 相似度 > MATCH_THRESHOLD → 返回已知说话人
- 相似度 < MATCH_THRESHOLD → 创建新声纹 "未知说话人-N"
- 声纹库持久化到 SQLite（同 echo.db 目录）

注意：
- 本模块在线程池中运行（非 asyncio），pipeline 通过 run_in_executor 调用
- 模型首次加载约需 2-5 秒，之后缓存在内存中
"""
from __future__ import annotations

import io
import json
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

# ── 可调参数（运行时从 config 读取，此处为模块级默认值）────────────────────────
DB_PATH = Path(__file__).parent.parent.parent / "echo.db"
SAMPLE_RATE = 16000      # 输入音频采样率（与前端/ESP32 一致）


def _get_threshold() -> float:
    """从 config 动态读取声纹匹配阈值（允许运行时通过 .env 调整）。"""
    try:
        from app.config import get_config
        return get_config().DIARIZER_MATCH_THRESHOLD
    except Exception:
        return 0.82

# ── 模块级单例 ────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_encoder = None          # VoiceEncoder 实例
_profiles: dict[str, list[dict]] = {}  # device_id → [{speaker_id, label, embedding}]


# ── 数据库初始化 ──────────────────────────────────────────────────────────────

def _ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speaker_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT NOT NULL,
            speaker_id  TEXT NOT NULL UNIQUE,
            label       TEXT NOT NULL,
            embedding   BLOB NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def _load_profiles(device_id: str) -> list[dict]:
    """从 DB 加载该设备的声纹库到内存。"""
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT speaker_id, label, embedding FROM speaker_profiles WHERE device_id=?",
        (device_id,)
    ).fetchall()
    conn.close()
    profiles = []
    for speaker_id, label, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        profiles.append({"speaker_id": speaker_id, "label": label, "embedding": emb})
    return profiles


def _save_profile(device_id: str, speaker_id: str, label: str, embedding: np.ndarray) -> None:
    conn = _ensure_db()
    blob = embedding.astype(np.float32).tobytes()
    conn.execute("""
        INSERT INTO speaker_profiles (device_id, speaker_id, label, embedding)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(speaker_id) DO UPDATE SET
            label=excluded.label,
            embedding=excluded.embedding,
            updated_at=datetime('now')
    """, (device_id, speaker_id, label, blob))
    conn.commit()
    conn.close()


# ── 音频处理 ──────────────────────────────────────────────────────────────────

def _pcm16_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """Int16 PCM bytes → float32 numpy array in [-1, 1]。"""
    n = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{n}h", pcm_bytes[:n*2])
    arr = np.array(samples, dtype=np.float32) / 32768.0
    return arr


def _get_encoder():
    """Lazy-load VoiceEncoder（首次调用约 2-5s）。"""
    global _encoder
    if _encoder is None:
        from resemblyzer import VoiceEncoder
        _encoder = VoiceEncoder()
        logger.info("VoiceEncoder 加载完成")
    return _encoder


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def identify_speaker(audio_bytes: bytes, device_id: str) -> Optional[dict]:
    """
    识别音频中的说话人。

    参数：
        audio_bytes: Int16 PCM 字节，16kHz，单声道
        device_id:   设备 ID（用于隔离声纹库）

    返回：
        {"speaker_id": str, "label": str, "confidence": float}
        若音频太短无法提取 embedding 则返回 None
    """
    with _lock:
        try:
            wav = _pcm16_to_float32(audio_bytes)
            duration = len(wav) / SAMPLE_RATE

            # resemblyzer 要求至少约 1.6s 音频
            if duration < 1.0:
                logger.debug(f"[{device_id}] 音频太短 ({duration:.2f}s)，跳过声纹识别")
                return None

            encoder = _get_encoder()
            embedding = encoder.embed_utterance(wav)  # shape: (256,)

            # 加载/缓存声纹库
            if device_id not in _profiles:
                _profiles[device_id] = _load_profiles(device_id)

            profiles = _profiles[device_id]

            # 与已知声纹比对
            best_sim = -1.0
            best_profile = None
            for p in profiles:
                sim = float(np.dot(embedding, p["embedding"]) /
                            (np.linalg.norm(embedding) * np.linalg.norm(p["embedding"]) + 1e-8))
                if sim > best_sim:
                    best_sim = sim
                    best_profile = p

            threshold = _get_threshold()
            if best_profile and best_sim >= threshold:
                # 已知说话人：增量更新 embedding（指数移动平均，保持声纹新鲜）
                alpha = 0.1
                updated_emb = (1 - alpha) * best_profile["embedding"] + alpha * embedding
                updated_emb /= np.linalg.norm(updated_emb) + 1e-8
                best_profile["embedding"] = updated_emb
                _save_profile(device_id, best_profile["speaker_id"], best_profile["label"], updated_emb)

                return {
                    "speaker_id": best_profile["speaker_id"],
                    "label": best_profile["label"],
                    "confidence": best_sim,
                    "is_new": False,
                }
            else:
                # 未知说话人：创建新声纹
                idx = len(profiles) + 1
                speaker_id = f"{device_id}-spk{idx:03d}"
                label = f"未知说话人{idx}"
                norm_emb = embedding / (np.linalg.norm(embedding) + 1e-8)
                new_profile = {"speaker_id": speaker_id, "label": label, "embedding": norm_emb}
                profiles.append(new_profile)
                _save_profile(device_id, speaker_id, label, norm_emb)

                logger.info(f"[{device_id}] 新声纹注册: {speaker_id!r} ({label})")
                return {
                    "speaker_id": speaker_id,
                    "label": label,
                    "confidence": best_sim if best_sim > 0 else 0.0,
                    "is_new": True,
                }

        except Exception as e:
            logger.warning(f"[{device_id}] 声纹识别失败: {e}")
            return None


def rename_speaker(device_id: str, speaker_id: str, new_label: str) -> bool:
    """
    为已识别的声纹命名（人名、动物名、事件等）。
    由 LLM 工具或 API 调用。
    """
    with _lock:
        if device_id in _profiles:
            for p in _profiles[device_id]:
                if p["speaker_id"] == speaker_id:
                    p["label"] = new_label
                    _save_profile(device_id, speaker_id, new_label, p["embedding"])
                    logger.info(f"[{device_id}] 声纹重命名: {speaker_id} → {new_label!r}")
                    return True
        return False


def list_speakers(device_id: str) -> list[dict]:
    """返回该设备已知的所有说话人（不含 embedding）。"""
    with _lock:
        if device_id not in _profiles:
            _profiles[device_id] = _load_profiles(device_id)
        return [
            {"speaker_id": p["speaker_id"], "label": p["label"]}
            for p in _profiles[device_id]
        ]

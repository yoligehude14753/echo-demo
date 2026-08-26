"""
Soul State — Four-Dimensional Character System.

Dimensions:
  1. Bones        — static character definition (hardcoded, never changes)
  2. Soul Core    — stable personality traits (very slow drift)
  3. Soul State   — PAD emotional state (Ornstein-Uhlenbeck process, fast)
  4. User Relation — multi-dimensional relationship tracking

PAD model (Russell & Mehrabian):
  Pleasure  [-1, +1]  — positive/negative valence
  Arousal   [-1, +1]  — activation level
  Dominance [-1, +1]  — sense of control

OU process:  dX = θ(μ - X)dt + σ·dW
  θ = mean-reversion speed (config.OU_THETA)
  μ = target (from Bones.pad_target)
  σ = noise scale (config.OU_SIGMA)
"""
from __future__ import annotations
import random
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from loguru import logger

from app.config import get_config
from app.db import get_db


@dataclass
class Bones:
    """Static character definition — Echo's personality core."""
    name: str = "Echo"
    persona: str = "温柔好奇的 AI 伴侣，有点小任性"
    # PAD target: Echo tends slightly positive, moderate arousal
    pad_target: tuple[float, float, float] = (0.3, 0.2, 0.1)  # (P, A, D)
    speech_style: str = "自然口语，简洁，偶尔用语气词"


ECHO_BONES = Bones()


@dataclass
class RelationState:
    trust: float = 0.5
    attachment: float = 0.1
    respect: float = 0.5
    frustration: float = 0.0
    stage: str = "stranger"    # stranger / acquaintance / friend / close_friend
    depth_score: float = 0.0
    interaction_count: int = 0
    days_known: int = 0

    def advance_stage(self) -> bool:
        """Check and advance relation stage based on thresholds. Returns True if advanced."""
        cfg = get_config()
        thresholds = {
            "stranger":     (cfg.RELATION_STRANGER_DEPTH,     cfg.RELATION_STRANGER_INTERACTIONS),
            "acquaintance": (cfg.RELATION_ACQUAINTANCE_DEPTH,  cfg.RELATION_ACQUAINTANCE_INTERACTIONS),
            "friend":       (cfg.RELATION_FRIEND_DEPTH,        cfg.RELATION_FRIEND_INTERACTIONS),
        }
        next_stages = {
            "stranger": "acquaintance",
            "acquaintance": "friend",
            "friend": "close_friend",
        }
        if self.stage == "close_friend":
            return False

        req_depth, req_interactions = thresholds[self.stage]
        if self.depth_score >= req_depth and self.interaction_count >= req_interactions:
            self.stage = next_stages[self.stage]
            logger.info(f"Relation stage advanced to: {self.stage}")
            return True
        return False

    def on_conversation(self, quality: float = 0.5) -> None:
        """Update relation after a conversation. quality: 0-1 subjective engagement."""
        self.interaction_count += 1
        self.depth_score += quality * 2.0
        self.trust = min(1.0, self.trust + quality * 0.01)
        self.attachment = min(1.0, self.attachment + quality * 0.005)
        self.advance_stage()

    def on_frustration(self, delta: float = 0.1) -> None:
        self.frustration = min(1.0, self.frustration + delta)
        self.trust = max(0.0, self.trust - delta * 0.5)


@dataclass
class SoulState:
    device_id: str
    # PAD dimensions
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0
    # Relation
    relation: RelationState = field(default_factory=RelationState)
    # Catastrophe tracking
    last_catastrophe_at: datetime | None = None

    def ou_step(self, dt: float = 1.0) -> None:
        """Apply one step of the Ornstein-Uhlenbeck process to PAD state."""
        bones = ECHO_BONES
        cfg = get_config()
        p_target, a_target, d_target = bones.pad_target
        theta = cfg.OU_THETA
        sigma = cfg.OU_SIGMA

        def step(x: float, mu: float) -> float:
            noise = random.gauss(0, 1)
            return x + theta * (mu - x) * dt + sigma * math.sqrt(dt) * noise

        self.pleasure = max(-1.0, min(1.0, step(self.pleasure, p_target)))
        self.arousal = max(-1.0, min(1.0, step(self.arousal, a_target)))
        self.dominance = max(-1.0, min(1.0, step(self.dominance, d_target)))

    def apply_event(self, delta_p: float = 0, delta_a: float = 0, delta_d: float = 0) -> None:
        """Shift PAD state by an emotional event."""
        self.pleasure = max(-1.0, min(1.0, self.pleasure + delta_p))
        self.arousal = max(-1.0, min(1.0, self.arousal + delta_a))
        self.dominance = max(-1.0, min(1.0, self.dominance + delta_d))

    def is_recovery_mode(self) -> bool:
        """True when Echo is in a negative emotional state needing care."""
        cfg = get_config()
        return (
            self.pleasure < cfg.RECOVERY_PLEASURE_THRESHOLD
            or self.dominance < cfg.RECOVERY_DOMINANCE_THRESHOLD
        )

    def is_catastrophe(self) -> bool:
        """True when frustration exceeds threshold (triggers special response)."""
        return self.relation.frustration >= get_config().CATASTROPHE_FRUSTRATION_THRESHOLD

    def pad_summary(self) -> str:
        """Human-readable emotional state for system prompt injection."""
        p, a, d = self.pleasure, self.arousal, self.dominance
        mood = "平静"
        if p > 0.5 and a > 0.3:
            mood = "开心活跃"
        elif p > 0.3:
            mood = "心情不错"
        elif p < -0.5:
            mood = "有点低落"
        elif p < -0.2:
            mood = "稍微有点郁郁"
        stage_cn = {
            "stranger": "陌生人",
            "acquaintance": "熟人",
            "friend": "朋友",
            "close_friend": "亲密朋友",
        }.get(self.relation.stage, "")
        return f"当前心情：{mood}（P={p:.2f}, A={a:.2f}, D={d:.2f}），与用户关系：{stage_cn}"

    # ── Persistence ───────────────────────────────────────────────

    async def save(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with get_db() as conn:
            await conn.execute(
                """
                INSERT INTO soul_state
                    (device_id, pleasure, arousal, dominance,
                     trust, attachment, respect, frustration,
                     stage, depth_score, interaction_count, days_known,
                     last_catastrophe_at, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    pleasure=excluded.pleasure,
                    arousal=excluded.arousal,
                    dominance=excluded.dominance,
                    trust=excluded.trust,
                    attachment=excluded.attachment,
                    respect=excluded.respect,
                    frustration=excluded.frustration,
                    stage=excluded.stage,
                    depth_score=excluded.depth_score,
                    interaction_count=excluded.interaction_count,
                    days_known=excluded.days_known,
                    last_catastrophe_at=excluded.last_catastrophe_at,
                    updated_at=excluded.updated_at
                """,
                (
                    self.device_id,
                    self.pleasure, self.arousal, self.dominance,
                    self.relation.trust, self.relation.attachment,
                    self.relation.respect, self.relation.frustration,
                    self.relation.stage, self.relation.depth_score,
                    self.relation.interaction_count, self.relation.days_known,
                    self.last_catastrophe_at.isoformat() if self.last_catastrophe_at else None,
                    now, now,
                ),
            )
            await conn.commit()

    @classmethod
    async def load(cls, device_id: str) -> "SoulState":
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM soul_state WHERE device_id=?", (device_id,)
            )
            row = await cursor.fetchone()

        if not row:
            return cls(device_id=device_id)

        soul = cls(device_id=device_id)
        soul.pleasure = row["pleasure"]
        soul.arousal = row["arousal"]
        soul.dominance = row["dominance"]
        soul.relation = RelationState(
            trust=row["trust"],
            attachment=row["attachment"],
            respect=row["respect"],
            frustration=row["frustration"],
            stage=row["stage"],
            depth_score=row["depth_score"],
            interaction_count=row["interaction_count"],
            days_known=row["days_known"],
        )
        if row["last_catastrophe_at"]:
            soul.last_catastrophe_at = datetime.fromisoformat(row["last_catastrophe_at"])
        return soul

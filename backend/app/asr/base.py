"""
Unified ASR output model — consumed by pipeline, router, and memory nodes.

All ASR backends must emit TranscriptSegment objects so the rest of the
pipeline is decoupled from the underlying STT provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TranscriptSegment:
    """A single finalized utterance from the ASR pipeline."""

    # ── Core text ────────────────────────────────────────────────
    text: str
    confidence: float  # 0.0–1.0; Deepgram channel.alternatives[0].confidence

    # ── Speaker ──────────────────────────────────────────────────
    # Deepgram assigns temporary per-connection labels like "SPEAKER_0".
    # SpeakerResolver maps these to stable UUIDs stored in speaker_profiles.
    speaker_label: str = "SPEAKER_0"   # Deepgram diarization label
    speaker_uuid: str | None = None    # resemblyzer-mapped persistent UUID

    # ── Timing (seconds within the audio stream) ─────────────────
    start: float = 0.0
    end: float = 0.0

    # ── Origin ───────────────────────────────────────────────────
    source: str = "device"       # "device" (ESP32) | "desktop"
    device_id: str = "default"

    # ── Timestamps ───────────────────────────────────────────────
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── Routing result (set by Router node) ──────────────────────
    router_action: str | None = None   # "activate" | "ambient" | "ignore"

    # ── Convenience ──────────────────────────────────────────────
    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def is_valid(self, min_chars: int = 3) -> bool:
        """Reject empty or suspiciously short segments."""
        return bool(self.text and len(self.text.strip()) >= min_chars)

    def to_db_row(self) -> dict:
        return {
            "device_id":            self.device_id,
            "text":                 self.text.strip(),
            "speaker_label":        self.speaker_label,
            "speaker_uuid":         self.speaker_uuid,
            "confidence":           self.confidence,
            "source":               self.source,
            "router_action":        self.router_action,
            "recorded_at":          self.recorded_at,
            "processed_for_memory": 0,
        }

    def __repr__(self) -> str:
        return (
            f"<TranscriptSegment [{self.source}/{self.device_id}] "
            f"{self.speaker_label}({self.speaker_uuid or '?'}) "
            f"conf={self.confidence:.2f} "
            f'"{self.text[:40]}{"…" if len(self.text) > 40 else ""}">'
        )

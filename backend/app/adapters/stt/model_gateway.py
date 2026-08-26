"""唯一 STT adapter：通过 Model Gateway 流式调用 Qwen3-ASR。

输入音频在 adapter 边界规范化为 16kHz/16bit/mono WAV，结果只接收
ModelGatewayClient 返回的 partial/final 安全文本与 metadata。

稳定性策略：不在 adapter 层做本地熔断。语音识别服务偶发慢/空/断连时，调用方
按单次失败处理；ambient pipeline 负责并发闸，避免慢请求堆积。
"""

from __future__ import annotations

import logging
import time

from yoli_llm.model_gateway import (
    GatewayTranscriptionChunk,
    ModelGatewayClient,
    ModelGatewayError,
)

from app.adapters.audio import normalize_audio_bytes, pcm_to_wav
from app.adapters.audio_gate import repetitive_transcript_reason
from app.adapters.llm.capability_resolver import resolve_gateway_capability
from app.adapters.llm.model_gateway_factory import create_model_gateway_client
from app.config import Settings
from app.ports.stt import TranscriptStreamEvent, TranscriptStreamHandler
from app.schemas.meeting import TranscriptSegment

logger = logging.getLogger("echodesk.stt.model_gateway")


class STTError(RuntimeError):
    """Safe STT-facing failure which retains only error category and status."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status = status


class ModelGatewaySTT:
    """实现 ports.stt.STTPort。"""

    def __init__(self, settings: Settings, *, timeout_s: float | None = None,
                 gateway_client: ModelGatewayClient | None = None) -> None:
        self._settings = settings
        self._timeout = settings.model_gateway_timeout_s if timeout_s is None else timeout_s
        self._gateway = None
        self._gateway_config = None
        try:
            self._gateway, self._gateway_config = create_model_gateway_client(
                settings, gateway_client, total_timeout=self._timeout,
            )
        except RuntimeError:
            # Credential recovery is performed by the scheduler from fresh
            # capability evidence; construction must not permanently disable
            # the provider after a transient startup miss.
            if gateway_client is not None:
                raise
        self._default_language = settings.stt_language
        self._model = settings.stt_model
        self._fail_count = 0

    @staticmethod
    async def _notify_stream(
        handler: TranscriptStreamHandler | None,
        event: TranscriptStreamEvent,
    ) -> None:
        if handler is None:
            return
        try:
            await handler(event)
        except Exception as error:
            logger.warning(
                "asr stream projection failed state=%s error=%s",
                event.state,
                type(error).__name__,
            )

    async def refresh_capability(self) -> bool:
        """Refresh transcription eligibility without submitting audio."""
        if self._gateway is None:
            try:
                self._gateway, self._gateway_config = create_model_gateway_client(
                    self._settings, total_timeout=self._timeout,
                )
            except RuntimeError:
                return False
        try:
            await resolve_gateway_capability(
                self._gateway,
                "transcription",
                requested_model=self._model,
                timeout_s=self._timeout,
            )
        except ModelGatewayError:
            return False
        return True

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
        language: str | None = None,
        prompt: str | None = None,
        response_format: str | None = None,
        timeout_s: float | None = None,
        on_partial: TranscriptStreamHandler | None = None,
    ) -> list[TranscriptSegment]:
        if not audio_bytes:
            return []
        normalized = normalize_audio_bytes(audio_bytes, sample_rate=sample_rate)
        audio_bytes = normalized.pcm
        sample_rate = normalized.sample_rate
        wav = pcm_to_wav(audio_bytes, sample_rate=sample_rate)
        request_timeout = self._timeout if timeout_s is None else min(self._timeout, timeout_s)
        t0 = time.monotonic()
        capability = None
        try:
            if self._gateway is None:
                self._gateway, self._gateway_config = create_model_gateway_client(
                    self._settings, total_timeout=self._timeout,
                )
            capability = await resolve_gateway_capability(
                self._gateway,
                "transcription",
                requested_model=self._model,
                timeout_s=request_timeout,
            )
            options = {"language": language or self._default_language}
            if prompt is not None:
                options["prompt"] = prompt
            if response_format is not None:
                options["response_format"] = response_format
            partial_text = ""
            final_text: str | None = None
            final_metadata = None
            async for chunk in self._gateway.iter_transcription(
                wav,
                filename="audio.wav",
                mime="audio/wav",
                options=capability.options(options),
                policy=capability.policy,
                timeout_s=request_timeout,
            ):
                if (
                    not isinstance(chunk, GatewayTranscriptionChunk)
                    or not isinstance(chunk.text, str)
                    or not isinstance(chunk.is_final, bool)
                ):
                    raise TypeError
                if chunk.is_final:
                    final_text = chunk.text or partial_text
                    final_metadata = chunk.metadata
                    continue
                if not chunk.text:
                    continue
                partial_text += chunk.text
                repetition_reason = repetitive_transcript_reason(partial_text)
                if repetition_reason is not None:
                    self._fail_count += 1
                    await self._notify_stream(
                        on_partial,
                        TranscriptStreamEvent(text="", state="failed"),
                    )
                    logger.warning(
                        "asr repetitive stream aborted model=%s endpoint=%s "
                        "reason=%s chars=%d",
                        capability.model,
                        capability.endpoint,
                        repetition_reason,
                        len(partial_text),
                    )
                    raise STTError(
                        "repetitive gateway transcription output",
                        category="asr_repetitive_output",
                    )
                await self._notify_stream(
                    on_partial,
                    TranscriptStreamEvent(text=partial_text, state="partial"),
                )
            if final_text is None or final_metadata is None:
                raise TypeError
            text = final_text.strip()
            await self._notify_stream(
                on_partial,
                TranscriptStreamEvent(text=text, state="completed"),
            )
            logger.info(
                "asr transcription response model=%s endpoint=%s "
                "http_status=%s retry_count=%s request_id=%s route=%s text_length=%d",
                capability.model,
                capability.endpoint,
                final_metadata.http_status,
                final_metadata.retry_count,
                final_metadata.request_id,
                final_metadata.route,
                len(text),
            )
        except (TypeError, ValueError):
            self._fail_count += 1
            await self._notify_stream(
                on_partial,
                TranscriptStreamEvent(text="", state="failed"),
            )
            raise STTError("invalid gateway response") from None
        except ModelGatewayError as error:
            self._fail_count += 1
            await self._notify_stream(
                on_partial,
                TranscriptStreamEvent(text="", state="failed"),
            )
            logger.warning(
                "asr transcription failure model=%s endpoint=%s category=%s "
                "status=%s request_id=%s",
                capability.model if capability is not None else "unknown",
                capability.endpoint if capability is not None else "unknown",
                error.category,
                error.status,
                error.metadata.request_id if error.metadata is not None else None,
            )
            raise STTError(
                "model gateway request failed",
                category=error.category,
                status=error.status,
            ) from None
        except STTError:
            raise
        except Exception:
            self._fail_count += 1
            await self._notify_stream(
                on_partial,
                TranscriptStreamEvent(text="", state="failed"),
            )
            raise STTError("model gateway request failed") from None

        self._fail_count = 0
        if not text:
            return []

        # ``end_ms`` 是音频时间轴，不能混入 HTTP/推理 wall time。旧实现取
        # max(audio_duration, elapsed)，上游慢 20~60s 时会把一个 6s chunk
        # 伪装成 20~60s 的有效语音，进而错误触发或续命 auto meeting。
        duration_ms = int(len(audio_bytes) / (sample_rate * 2) * 1000)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.debug(
            "asr transcription completed audio_duration_ms=%d elapsed_ms=%d chars=%d",
            duration_ms,
            elapsed_ms,
            len(text),
        )
        return [
            TranscriptSegment(
                text=text,
                start_ms=0,
                end_ms=duration_ms,
                speaker_id=None,
                speaker_label=None,
            )
        ]

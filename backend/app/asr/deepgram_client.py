"""
Deepgram Nova-3 Streaming ASR Client — SDK v6 compatible.

Architecture:
- One persistent WebSocket connection per (device_id, source) pair.
- Audio bytes are fed via push_audio(); final TranscriptSegments are
  delivered through the async callback on_segment.
- Server-side VAD: Deepgram's `endpointing` parameter handles silence
  detection, eliminating hallucinations from silence.
- Speaker diarization: Deepgram returns word-level speaker indices;
  SpeakerResolver maps them to stable UUIDs later.

SDK v6 connection lifecycle:
  connect() → async context manager kept open → push_audio() ...
  → close() → finalize + exit ctx
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from app.asr.base import TranscriptSegment
from app.config import get_config

if TYPE_CHECKING:
    pass

try:
    from deepgram import AsyncDeepgramClient
    from deepgram.core.events import EventType
    from deepgram.listen import ListenV1Results
    DEEPGRAM_AVAILABLE = True
except ImportError:
    DEEPGRAM_AVAILABLE = False
    logger.warning("deepgram-sdk not installed — Deepgram ASR unavailable")


SegmentCallback = Callable[[TranscriptSegment], Awaitable[None]]

_MAX_RETRIES = 5
_RETRY_BASE_S = 0.3   # fast first reconnect; still exponential after


class DeepgramASRClient:
    """
    Manages a streaming ASR connection for a single audio source.

    Usage::

        async def on_seg(seg: TranscriptSegment):
            print(seg)

        client = DeepgramASRClient("device_01", "device", on_seg)
        await client.connect()
        await client.push_audio(pcm_bytes)
        ...
        await client.close()
    """

    def __init__(
        self,
        device_id: str,
        source: str,            # "device" | "desktop"
        on_segment: SegmentCallback,
    ) -> None:
        self.device_id = device_id
        self.source = source
        self.on_segment = on_segment

        self._cfg = get_config()
        self._connection = None      # AsyncV1SocketClient
        self._ctx = None             # async context manager
        self._listen_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._connected = False
        self._closing = False
        self._reconnecting = False   # guard against concurrent reconnect tasks
        self._retry_count = 0
        self._last_audio_time: float = 0.0  # monotonic time of last push_audio call

    # ── Public API ────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the WebSocket connection to Deepgram."""
        if not DEEPGRAM_AVAILABLE:
            raise RuntimeError(
                "deepgram-sdk is not installed. "
                "Run: pip install 'deepgram-sdk>=6'"
            )
        await self._connect_inner()

    async def push_audio(self, pcm: bytes) -> None:
        """Send raw PCM bytes (16kHz, 16-bit, mono LE) to Deepgram."""
        self._last_audio_time = time.monotonic()
        if self._connected and self._connection and not self._closing:
            try:
                await self._connection.send_media(pcm)
            except Exception as exc:
                logger.warning(f"[ASR] send_media error ({self.device_id}): {exc}")
                if not self._closing:
                    asyncio.create_task(self._try_reconnect())

    async def close(self) -> None:
        """Gracefully shut down the connection."""
        self._closing = True
        self._connected = False

        for task in (self._keepalive_task, self._listen_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._keepalive_task = None

        if self._connection:
            try:
                await self._connection.send_finalize()
            except Exception:
                pass

        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass

        self._connection = None
        self._ctx = None
        self._listen_task = None
        logger.info(f"[ASR] Closed connection: {self.device_id}/{self.source}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ──────────────────────────────────────────────────

    async def _connect_inner(self) -> None:
        cfg = self._cfg
        api_key = cfg.DEEPGRAM_API_KEY
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY is not configured")

        dg = AsyncDeepgramClient(api_key=api_key)

        # v6: all options are passed as keyword args to connect()
        # Booleans must be passed as strings per the Fern-generated client
        connect_kwargs: dict = {
            "model": "nova-3",
            "language": cfg.DEEPGRAM_LANGUAGE,
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "punctuate": "true" if cfg.DEEPGRAM_PUNCTUATE else "false",
            "diarize": "true" if cfg.DEEPGRAM_DIARIZE else "false",
            "endpointing": str(cfg.DEEPGRAM_ENDPOINTING_MS),
            "interim_results": "true" if cfg.DEEPGRAM_INTERIM_RESULTS else "false",
            "smart_format": "true",
        }
        self._ctx = dg.listen.v1.connect(**connect_kwargs)

        # Enter context manager — keeps the WS open
        self._connection = await self._ctx.__aenter__()

        # Register event handlers (sync wrapper → async task)
        def _on_message(message) -> None:
            if isinstance(message, ListenV1Results):
                asyncio.create_task(self._handle_result(message))

        def _on_error(error) -> None:
            logger.error(f"[ASR] Deepgram error ({self.device_id}): {error}")
            if not self._closing:
                asyncio.create_task(self._try_reconnect())

        def _on_close(_) -> None:
            if not self._closing:
                logger.warning(
                    f"[ASR] Connection closed unexpectedly: "
                    f"{self.device_id}/{self.source}"
                )
                asyncio.create_task(self._try_reconnect())

        self._connection.on(EventType.MESSAGE, _on_message)
        self._connection.on(EventType.ERROR,   _on_error)
        self._connection.on(EventType.CLOSE,   _on_close)

        # start_listening() is a blocking async loop — run as background task
        self._listen_task = asyncio.create_task(
            self._connection.start_listening(),
            name=f"asr-listen-{self.device_id}-{self.source}",
        )

        # Keepalive: send KeepAlive every 5s to prevent Deepgram 10s idle timeout
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(),
            name=f"asr-ka-{self.device_id}-{self.source}",
        )

        self._connected = True
        self._retry_count = 0
        logger.info(
            f"[ASR] Connected to Deepgram Nova-3: {self.device_id}/{self.source} "
            f"lang={cfg.DEEPGRAM_LANGUAGE} endpoint={cfg.DEEPGRAM_ENDPOINTING_MS}ms "
            f"diarize={cfg.DEEPGRAM_DIARIZE}"
        )

    async def _handle_result(self, result: ListenV1Results) -> None:
        """Process a final transcript result from Deepgram."""
        try:
            if not result.is_final:
                return

            channel = result.channel
            if not channel or not channel.alternatives:
                return

            alt = channel.alternatives[0]
            text = (alt.transcript or "").strip()
            if not text:
                return

            confidence = float(alt.confidence or 1.0)

            # Extract dominant speaker from word-level diarization
            speaker_label = "SPEAKER_0"
            words = alt.words or []
            if words:
                counts = Counter(
                    w.speaker
                    for w in words
                    if w.speaker is not None
                )
                if counts:
                    speaker_label = f"SPEAKER_{counts.most_common(1)[0][0]}"

            start_s = float(result.start or 0.0)
            duration_s = float(result.duration or 0.0)

            seg = TranscriptSegment(
                text=text,
                confidence=confidence,
                speaker_label=speaker_label,
                speaker_uuid=None,   # SpeakerResolver fills this later
                start=start_s,
                end=start_s + duration_s,
                source=self.source,
                device_id=self.device_id,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )

            logger.debug(f"[ASR] {seg}")

            try:
                await self.on_segment(seg)
            except Exception as exc:
                logger.exception(f"[ASR] on_segment callback error: {exc}")

        except Exception as exc:
            logger.exception(f"[ASR] transcript parse error: {exc}")

    async def _keepalive_loop(self) -> None:
        """Send silence PCM every 3s when idle to prevent Deepgram 10s timeout.

        Silence is more reliable than send_keepalive() for keeping the connection
        alive: Deepgram accepts real audio data even if it's silent.
        """
        _SILENCE_100MS = b"\x00" * 3200  # 100ms @ 16kHz 16-bit mono
        try:
            while not self._closing:
                await asyncio.sleep(3.0)
                if self._closing or not self._connected or not self._connection:
                    break
                idle_s = time.monotonic() - self._last_audio_time
                if idle_s >= 1.0:
                    try:
                        await self._connection.send_media(_SILENCE_100MS)
                    except Exception:
                        # Connection broken; _on_close will handle reconnect
                        break
        except asyncio.CancelledError:
            pass

    async def _try_reconnect(self) -> None:
        # Prevent concurrent reconnect attempts
        if self._reconnecting:
            return
        if self._closing or self._retry_count >= _MAX_RETRIES:
            logger.error(
                f"[ASR] Max retries reached for "
                f"{self.device_id}/{self.source}. Giving up."
            )
            return

        # Skip reconnect if no audio has ever been sent to this client (device idle).
        # push_audio() → get_or_create_client() will open a fresh connection on demand.
        idle_s = time.monotonic() - self._last_audio_time
        if self._last_audio_time == 0.0 or idle_s > 30.0:
            logger.info(
                f"[ASR] Skipping reconnect for idle client "
                f"{self.device_id}/{self.source} "
                f"(no audio for {idle_s:.0f}s) — will reconnect on next push_audio"
            )
            self._connected = False
            self._retry_count = 0
            return

        self._reconnecting = True
        self._connected = False
        delay = _RETRY_BASE_S * (2 ** self._retry_count)
        self._retry_count += 1
        logger.info(
            f"[ASR] Reconnecting in {delay:.1f}s "
            f"(attempt {self._retry_count}/{_MAX_RETRIES})"
        )
        try:
            await asyncio.sleep(delay)

            # Check again after sleep — close() may have been called while sleeping
            if self._closing:
                return

            # Clean up old context
            for task in (self._keepalive_task, self._listen_task):
                if task and not task.done():
                    task.cancel()
            self._keepalive_task = None
            if self._ctx:
                try:
                    await self._ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            self._ctx = None
            self._connection = None

            try:
                await self._connect_inner()
            except Exception as exc:
                logger.error(f"[ASR] Reconnect failed: {exc}")
        finally:
            self._reconnecting = False


# ── Connection Registry ────────────────────────────────────────────────────────

_registry: dict[str, DeepgramASRClient] = {}
_registry_lock = asyncio.Lock()


async def get_or_create_client(
    device_id: str,
    source: str,
    on_segment: SegmentCallback,
) -> DeepgramASRClient:
    """Return an existing connected client or create + connect a new one."""
    key = f"{device_id}:{source}"
    async with _registry_lock:
        client = _registry.get(key)
        if client and client.is_connected:
            return client
        # Old client exists but is disconnected — close it silently before replacing
        if client and not client.is_connected:
            asyncio.create_task(client.close())
        client = DeepgramASRClient(device_id, source, on_segment)
        await client.connect()
        _registry[key] = client
    return client


async def close_client(device_id: str, source: str) -> None:
    """Close and remove the client for a given device/source."""
    key = f"{device_id}:{source}"
    async with _registry_lock:
        client = _registry.pop(key, None)
    if client:
        await client.close()


async def close_all_clients() -> None:
    """Close all active connections (called on server shutdown)."""
    async with _registry_lock:
        keys = list(_registry.keys())
        clients = [_registry.pop(k) for k in keys]
    for c in clients:
        try:
            await c.close()
        except Exception:
            pass

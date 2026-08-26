"""
Core dialogue pipeline — Deepgram-first ASR architecture.

Audio data flow (Deepgram mode):
  audio_bytes ──push_audio()──► Deepgram Nova-3 WebSocket
                                  (server-side VAD + endpointing)
                                        │
                                  TranscriptSegment (final only)
                                        │
                               handle_transcript()
                                        │
                              Router LLM: activate / ambient / ignore
                               ┌─────────┴──────────┐
                           activate              ambient/ignore
                               │                     │
                          _dialogue()       Memory Node → ambient_transcripts
                               │
                      LLM → broadcast → TTS (if enabled)

Text input (desktop):
  handle_text() → _dialogue() directly (skip Router)

Backward compat:
  handle_audio() still exists for non-Deepgram backends (api/funasr/faster_whisper).
  When STT_BACKEND="deepgram", ws_gateway should call push_audio() per chunk.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from loguru import logger

from app.config import get_config
from app.tts import synthesize, synthesize_stream
from app.llm import stream_complete
from app.session.manager import SessionManager
from app.session.prompt_builder import build_system_prompt
from app.memory.graph import MemoryGraph
from app.memory.retriever import retrieve_context
from app.memory.extractor import extract_from_conversation, save_extraction
from app.soul.state import SoulState
from app.persona.filler import inject_filler
from app.persona.mode_router import route_mode
from app.tools.registry import get_tools, get_handlers
from app.llm import complete_with_tools, complete_with_fallback
from app.file_context import fetch_workspace_context
from app.asr.base import TranscriptSegment
from app.nodes.router import route as router_classify, route_fast
from app.db import get_db
from app.nodes.memory import handle_ambient

# ── Sentence boundary detection for TTS streaming ────────────────
_SENTENCE_END_RE = re.compile(r'[。！？!?\n]')
_MIN_SENTENCE_LEN = 6   # chars; shorter fragments wait to be joined

# ── Module-level state ────────────────────────────────────────────

# device_id → active SessionManager
_sessions: dict[str, SessionManager] = {}
# device_id → MemoryGraph (shared per device)
_graphs: dict[str, MemoryGraph] = {}
# device_id → SoulState
_souls: dict[str, SoulState] = {}
# device_id → TTS enabled flag
_tts_enabled: dict[str, bool] = {}
# device_id → last TranscriptSegment timestamp (UTC) — used by Dream idle trigger
_last_transcript_at: dict[str, datetime] = {}
# device_id → TTS 结束时间戳（用于播放后冷却期，防止 TTS 余音触发反馈环路）
_tts_completed_at: dict[str, float] = {}
# device_id → 当前活跃的 dialogue 任务（含 LLM + TTS），用于 barge-in 取消
_active_dialogue_tasks: dict[str, asyncio.Task] = {}

# ── Dialogue follow-up window ─────────────────────────────────────
# After an activate, any speech from the same speaker within this window
# is automatically treated as dialogue (no wake word required).
_DIALOGUE_WINDOW_S: float = 45.0
_dialogue_window_until: dict[str, float] = {}   # device_id → monotonic expiry
_dialogue_speaker: dict[str, str | None] = {}   # device_id → speaker label

# TTS 完成后冷却时间（秒）
# 缩短到 1s：barge-in 已能主动取消 TTS，1s 仅作为 AEC 保护裕量
TTS_AUDIO_COOLDOWN_S: float = 1.0


# ── Background buffer: batches non-activate transcripts ──────────

class BackgroundBuffer:
    """
    Per-device buffer for personal/ambient transcripts.

    Eliminates per-segment Router LLM calls for background conversation.
    Drains automatically when count >= MAX_SIZE or idle >= IDLE_SECS.
    Draining just calls handle_ambient() for each buffered segment (no LLM).
    """
    MAX_SIZE: int = 20
    IDLE_SECS: float = 20.0

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self._queue: list[TranscriptSegment] = []
        self._idle_task: asyncio.Task | None = None

    def add(self, seg: TranscriptSegment) -> None:
        self._queue.append(seg)
        if len(self._queue) >= self.MAX_SIZE:
            self._cancel_idle_timer()
            asyncio.create_task(self._drain())
        else:
            self._reset_idle_timer()

    def _reset_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_task = asyncio.create_task(self._idle_drain())

    def _cancel_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_drain(self) -> None:
        try:
            await asyncio.sleep(self.IDLE_SECS)
            await self._drain()
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        if not self._queue:
            return
        segs, self._queue = self._queue[:], []
        logger.info(f"[{self.device_id}] BackgroundBuffer draining {len(segs)} segments")
        for seg in segs:
            try:
                await handle_ambient(seg, None)
            except Exception as exc:
                logger.warning(f"[{self.device_id}] BackgroundBuffer drain error: {exc}")

    async def flush(self) -> None:
        """Force drain (e.g. on device disconnect)."""
        self._cancel_idle_timer()
        await self._drain()


# device_id → BackgroundBuffer
_bg_buffers: dict[str, BackgroundBuffer] = {}


def _get_bg_buffer(device_id: str) -> BackgroundBuffer:
    if device_id not in _bg_buffers:
        _bg_buffers[device_id] = BackgroundBuffer(device_id)
    return _bg_buffers[device_id]


# ── State accessors ───────────────────────────────────────────────

def set_tts_enabled(device_id: str, enabled: bool) -> None:
    """Toggle TTS output for a device (called by ws_gateway)."""
    _tts_enabled[device_id] = enabled
    logger.info(f"[{device_id}] TTS {'enabled' if enabled else 'disabled'}")


def is_tts_enabled(device_id: str) -> bool:
    return _tts_enabled.get(device_id, True)  # ESP32 devices default to on


def mark_tts_completed(device_id: str) -> None:
    """TTS 发送完成时调用，启动后端冷却期。"""
    import time as _time
    _tts_completed_at[device_id] = _time.monotonic()


def is_in_tts_cooldown(device_id: str) -> bool:
    """冷却期内返回 True，期间忽略该设备上传的音频，防止 TTS 余音触发反馈环路。"""
    import time as _time
    completed = _tts_completed_at.get(device_id, 0.0)
    return (_time.monotonic() - completed) < TTS_AUDIO_COOLDOWN_S


def get_session(device_id: str) -> SessionManager:
    if device_id not in _sessions:
        _sessions[device_id] = SessionManager(device_id)
        logger.info(f"New session started for device: {device_id}")
    return _sessions[device_id]


def get_graph(device_id: str) -> MemoryGraph:
    if device_id not in _graphs:
        _graphs[device_id] = MemoryGraph(device_id=device_id)
    return _graphs[device_id]


async def get_soul(device_id: str) -> SoulState:
    if device_id not in _souls:
        _souls[device_id] = await SoulState.load(device_id)
    return _souls[device_id]


def clear_device_state(device_id: str) -> None:
    """Reset in-memory state for a device (used in tests)."""
    _sessions.pop(device_id, None)
    _graphs.pop(device_id, None)
    _souls.pop(device_id, None)
    _last_transcript_at.pop(device_id, None)


# ── Pipeline class ────────────────────────────────────────────────

class Pipeline:
    def __init__(self, device_id: str, manager) -> None:
        self.device_id = device_id
        self.manager = manager
        self.session = get_session(device_id)
        self.graph = get_graph(device_id)

    # ── Main ASR entry point (Deepgram callback) ──────────────────

    async def handle_transcript(
        self,
        seg: TranscriptSegment,
        audio_bytes: bytes | None = None,
    ) -> None:
        """
        Dual-track transcript processing.

        ACTIVATE TRACK (low-latency, ~500-700ms to first audio):
          Layer 0: noise/length filter     → ignore  (0ms, no API)
          Layer 1: wake word detection     → activate (0ms, skip Router LLM)
          Layer 2: structural heuristic    → personal/ambient (0ms, skip Router LLM)
          Layer 3: uncertain               → Router LLM (200-400ms, only ~20% of segments)
          → LLM streaming + sentence-level TTS pipeline

        BACKGROUND TRACK (batch, no real-time LLM):
          personal/ambient segments → BackgroundBuffer (drains every 20s or 20 segs)
          → handle_ambient() batch storage
          → Router LLM completely eliminated for background conversation

        Barge-in: new activate while TTS playing → cancel active dialogue task
                  → send audio_cancel to device → start new dialogue
        """
        _last_transcript_at[self.device_id] = datetime.now(timezone.utc)

        if not seg.is_valid(get_config().ROUTER_MIN_TEXT_LEN):
            logger.debug(f"[{self.device_id}] transcript too short → ignore: '{seg.text}'")
            return

        t_transcript = time.monotonic()

        # ── Layer 0-2: rule-based fast-path (0ms) ────────────────
        fast_action = route_fast(seg)

        if fast_action == "ignore":
            logger.debug(f"[{self.device_id}] fast-path ignore: '{seg.text[:40]}'")
            return

        soul = await get_soul(self.device_id)

        # ── Dialogue follow-up window check (covers personal/ambient/uncertain) ──
        # Within the post-activate window, ANY speech from the same speaker
        # is treated as continued dialogue — skips Router LLM entirely.
        if fast_action != "activate":
            window_until = _dialogue_window_until.get(self.device_id, 0.0)
            if time.monotonic() < window_until:
                dlg_speaker = _dialogue_speaker.get(self.device_id)
                if dlg_speaker is None or seg.speaker_label == dlg_speaker:
                    logger.info(
                        f"[{self.device_id}] Follow-up dialogue (window active, "
                        f"spk={seg.speaker_label}): '{seg.text[:60]}'"
                    )
                    fast_action = "activate"

        if fast_action in ("personal", "ambient"):
            # Background track: buffer without Router LLM
            seg.router_action = fast_action
            logger.info(
                f"[{self.device_id}] [{seg.source}] bg={fast_action} "
                f"dt={time.monotonic()-t_transcript:.3f}s text='{seg.text[:60]}'"
            )
            await self.manager.broadcast_to_desktops(self.device_id, {
                "type": "transcript",
                "text": seg.text,
                "source": seg.source,
                "speaker": seg.speaker_label,
                "action": fast_action,
            })
            _get_bg_buffer(self.device_id).add(seg)
            return

        # ── Layer 3: uncertain or wake-word activate ──────────────
        if fast_action == "activate":
            # Wake word confirmed: skip Router LLM entirely
            action = "activate"
            prep_task = asyncio.create_task(self._prepare_llm_context(seg.text, soul))
        else:
            # fast_action is None: run Router LLM + context prep in parallel
            router_task = asyncio.create_task(router_classify(seg))
            prep_task   = asyncio.create_task(self._prepare_llm_context(seg.text, soul))
            action = await router_task
            seg.router_action = action

        logger.info(
            f"[{self.device_id}] [{seg.source}] router={action} "
            f"spk={seg.speaker_label} dt={time.monotonic()-t_transcript:.3f}s "
            f"text='{seg.text[:60]}'"
        )

        if action == "ignore":
            prep_task.cancel()
            return

        # Broadcast transcript bubble to desktop
        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "transcript",
            "text": seg.text,
            "source": seg.source,
            "speaker": seg.speaker_label,
            "action": action,
        })

        if action == "activate":
            # ── Open / refresh dialogue follow-up window ──────────────
            _dialogue_window_until[self.device_id] = (
                time.monotonic() + _DIALOGUE_WINDOW_S
            )
            _dialogue_speaker[self.device_id] = seg.speaker_label

            # ── Barge-in: cancel any in-progress dialogue/TTS ────
            existing = _active_dialogue_tasks.get(self.device_id)
            if existing and not existing.done():
                logger.info(f"[{self.device_id}] Barge-in: cancelling active dialogue")
                existing.cancel()
                # Notify device to flush its audio buffer immediately
                try:
                    await self.manager.send_to_device(
                        self.device_id, {"type": "audio_cancel"}, queue_if_offline=False
                    )
                except Exception:
                    pass

            context = await prep_task
            # Run dialogue as a tracked background task so barge-in can cancel it
            task = asyncio.create_task(self._dialogue_streaming(seg.text, soul, context))
            _active_dialogue_tasks[self.device_id] = task
            task.add_done_callback(
                lambda t: _active_dialogue_tasks.pop(self.device_id, None)
            )
        else:
            # personal/ambient from LLM router → background buffer (no LLM response)
            prep_task.cancel()
            seg.router_action = action
            asyncio.create_task(handle_ambient(seg, audio_bytes))
            _get_bg_buffer(self.device_id).add(seg)

    # ── Audio push (Deepgram streaming mode) ─────────────────────

    async def push_audio(self, pcm: bytes, source: str = "device") -> None:
        """
        Feed raw PCM bytes directly to the Deepgram streaming client.
        ws_gateway calls this per audio_chunk when STT_BACKEND="deepgram".
        Deepgram handles VAD server-side; we never call STT on silence.
        """
        from app.asr.deepgram_client import get_or_create_client

        async def _on_segment(seg: TranscriptSegment) -> None:
            await self.handle_transcript(seg)

        try:
            client = await get_or_create_client(self.device_id, source, _on_segment)
            await client.push_audio(pcm)
        except Exception as exc:
            logger.error(f"[{self.device_id}] push_audio failed: {exc}")

    # ── Legacy audio entry point (non-Deepgram backends) ─────────

    async def handle_audio(self, audio_bytes: bytes, source: str = "device") -> None:
        """
        Legacy path: call batch STT (api/funasr/faster_whisper) then route.
        Only used when STT_BACKEND != "deepgram".
        """
        from app.stt import transcribe

        try:
            text = await transcribe(audio_bytes)
        except Exception as exc:
            logger.warning(f"[{self.device_id}] STT failed: {exc}")
            return

        if not text.strip():
            return

        seg = TranscriptSegment(
            text=text,
            confidence=1.0,
            source=source,
            device_id=self.device_id,
        )
        await self.handle_transcript(seg, audio_bytes)

    # ── Text input (desktop keyboard, no routing needed) ─────────

    async def handle_text(self, text: str) -> None:
        """Direct text input — skip Router and go straight to streaming dialogue."""
        if not text.strip():
            return
        soul = await get_soul(self.device_id)
        context = await self._prepare_llm_context(text, soul)
        await self._dialogue_streaming(text, soul, context)

    # ── Context preparation & streaming dialogue ──────────────────

    async def _load_profile(self) -> tuple[str, str]:
        """Load user_name and bio from device_profiles table. Returns (name, bio)."""
        try:
            async with get_db() as conn:
                cursor = await conn.execute(
                    "SELECT user_name, bio FROM device_profiles WHERE device_id=?",
                    (self.device_id,),
                )
                row = await cursor.fetchone()
            if row:
                return (row["user_name"] or "", row["bio"] or "")
        except Exception as e:
            logger.debug(f"profile load error: {e}")
        return ("", "")

    async def _prepare_llm_context(self, text: str, soul: SoulState) -> dict:
        """
        Parallel LLM context preparation — runs concurrently with Router.

        Gathers memory retrieval + workspace context simultaneously so the
        system prompt is ready the moment Router confirms "activate".
        Returns {"system_prompt": str, "mode": PersonaMode}.
        """
        (memory_ctx, file_ctx), (user_name, user_bio) = await asyncio.gather(
            asyncio.gather(retrieve_context(text, self.graph), fetch_workspace_context()),
            self._load_profile(),
        )
        soul_summary = soul.pad_summary() if soul else ""
        mode = route_mode(text, soul)
        system_prompt = build_system_prompt(
            session_notes=self.session.notes,
            memory_context=memory_ctx,
            soul_summary=soul_summary,
            file_context=file_ctx,
            user_bio=user_bio,
            user_name=user_name,
        )
        if mode.system_suffix:
            system_prompt += f"\n\n【当前对话模式】{mode.system_suffix}"
        return {"system_prompt": system_prompt, "mode": mode}

    async def _dialogue_streaming(
        self, text: str, soul: SoulState, context: dict
    ) -> None:
        """
        Sentence-level streaming LLM + TTS pipeline.

        Architecture:
          LLM token stream ──► sentence boundary detector ──► asyncio.Queue
                                                                    │
                                              TTS worker (concurrent)│
                                              ← dequeue sentence      │
                                              → synthesize_stream()   │
                                              → PCM chunks to device  │

        First audio reaches device ~500-700ms after user finishes speaking
        (vs. 2-4s with the previous wait-for-full-response approach).

        Barge-in: if this task is cancelled, TTS worker is also cancelled
        and the device receives an audio_cancel signal via handle_transcript.
        """
        logger.info(f"[{self.device_id}] User: {text}")
        self.session.add_turn("user", text)

        soul.ou_step()
        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "emotion",
            "pad": {
                "pleasure": soul.pleasure,
                "arousal": soul.arousal,
                "dominance": soul.dominance,
            },
        })

        messages = self.session.build_messages(context["system_prompt"])
        mode = context["mode"]
        full_response_parts: list[str] = []

        # TTS sentence queue: None sentinel signals worker to stop
        tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task: asyncio.Task | None = None

        if is_tts_enabled(self.device_id):
            tts_task = asyncio.create_task(self._run_tts_pipeline(tts_queue, soul))

        try:
            sentence_buf = ""

            # ── Streaming LLM path ────────────────────────────────
            try:
                async for token in stream_complete(messages):
                    if token:
                        full_response_parts.append(token)
                        sentence_buf += token
                        await self.manager.broadcast_to_desktops(self.device_id, {
                            "type": "response_chunk",
                            "text": token,
                        })
                        # Flush a sentence when boundary detected
                        if (
                            _SENTENCE_END_RE.search(sentence_buf)
                            and len(sentence_buf.strip()) >= _MIN_SENTENCE_LEN
                        ):
                            if tts_task:
                                await tts_queue.put(sentence_buf.strip())
                            sentence_buf = ""

                # Flush any remaining partial sentence
                if sentence_buf.strip() and len(sentence_buf.strip()) >= 2:
                    if tts_task:
                        await tts_queue.put(sentence_buf.strip())

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[{self.device_id}] stream_complete error ({exc}), falling back"
                )
                full_response_parts = []

            # ── Tool-call / error fallback ────────────────────────
            if not full_response_parts:
                logger.info(f"[{self.device_id}] Streaming empty → complete_with_tools")
                try:
                    fallback_resp = await complete_with_tools(
                        messages=messages,
                        tool_handlers=get_handlers(self.device_id),
                        tools=get_tools(),
                    )
                except Exception as exc:
                    logger.warning(f"[{self.device_id}] complete_with_tools failed ({exc})")
                    fallback_resp = await complete_with_fallback(messages=messages)

                resp_text = fallback_resp or "我刚才有点出神，你刚才说什么？"
                full_response_parts = [resp_text]
                await self.manager.broadcast_to_desktops(self.device_id, {
                    "type": "response_chunk",
                    "text": resp_text,
                })
                if tts_task:
                    await tts_queue.put(resp_text)

            # Signal TTS worker: all sentences enqueued
            if tts_task:
                await tts_queue.put(None)

            response = "".join(full_response_parts) or "我刚才有点出神，你刚才说什么？"

            if mode.filler_allowed:
                response = inject_filler(response, pleasure=soul.pleasure, arousal=soul.arousal)

            await self.manager.broadcast_to_desktops(self.device_id, {
                "type": "response",
                "text": response,
            })

            self.session.add_turn("assistant", response)
            soul.relation.on_conversation(quality=0.6)
            await self.session.maybe_update_notes()

            asyncio.create_task(self._extract_memory(text, response))
            asyncio.create_task(soul.save())

            # Wait for TTS pipeline to finish (with safety timeout)
            if tts_task and not tts_task.done():
                try:
                    await asyncio.wait_for(tts_task, timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.device_id}] TTS pipeline timeout, cancelling")
                    tts_task.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"[{self.device_id}] TTS pipeline error: {exc}")

            # Refresh dialogue window after response finishes so user can
            # continue conversation without saying "Echo" again.
            _dialogue_window_until[self.device_id] = (
                time.monotonic() + _DIALOGUE_WINDOW_S
            )

        except asyncio.CancelledError:
            # Barge-in: cancel TTS worker too
            if tts_task and not tts_task.done():
                tts_task.cancel()
                try:
                    await asyncio.wait_for(tts_task, timeout=1.0)
                except Exception:
                    pass
            raise

    # ── Legacy dialogue chain (kept for backward compat) ─────────

    async def _dialogue(self, text: str) -> None:
        """Full LLM → broadcast → conditional TTS flow."""
        logger.info(f"[{self.device_id}] User: {text}")
        self.session.add_turn("user", text)

        soul = await get_soul(self.device_id)
        soul.ou_step()

        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "emotion",
            "pad": {
                "pleasure": soul.pleasure,
                "arousal": soul.arousal,
                "dominance": soul.dominance,
            },
        })

        try:
            response = await self._llm_respond(text, soul)
        except Exception as exc:
            logger.exception(f"[{self.device_id}] LLM error: {exc}")
            response = "我刚才有点出神，你刚才说什么？"

        self.session.add_turn("assistant", response)
        soul.relation.on_conversation(quality=0.6)
        await self.session.maybe_update_notes()

        asyncio.create_task(self._extract_memory(text, response))
        asyncio.create_task(soul.save())

        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "response",
            "text": response,
        })

        if is_tts_enabled(self.device_id):
            tts_q: asyncio.Queue[str | None] = asyncio.Queue()
            await tts_q.put(response)
            await tts_q.put(None)
            await self._run_tts_pipeline(tts_q, soul)

    # ── LLM ───────────────────────────────────────────────────────

    async def _llm_respond(self, user_text: str, soul: SoulState | None = None) -> str:
        memory_ctx = await retrieve_context(user_text, self.graph)
        soul_summary = soul.pad_summary() if soul else ""
        mode = route_mode(user_text, soul)
        file_ctx = await fetch_workspace_context()

        user_name, user_bio = await self._load_profile()
        system_prompt = build_system_prompt(
            session_notes=self.session.notes,
            memory_context=memory_ctx,
            soul_summary=soul_summary,
            file_context=file_ctx,
            user_bio=user_bio,
            user_name=user_name,
        )
        if mode.system_suffix:
            system_prompt += f"\n\n【当前对话模式】{mode.system_suffix}"

        messages = self.session.build_messages(system_prompt)

        try:
            response = await complete_with_tools(
                messages=messages,
                tool_handlers=get_handlers(self.device_id),
                tools=get_tools(),
            )
        except Exception as exc:
            logger.warning(f"[{self.device_id}] primary LLM failed ({exc}), using fallback")
            response = await complete_with_fallback(messages=messages)

        if mode.filler_allowed and soul:
            response = inject_filler(response, pleasure=soul.pleasure, arousal=soul.arousal)

        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "response_chunk",
            "text": response,
        })

        logger.info(f"[{self.device_id}] LLM response: {response[:80]!r}")
        return response

    async def _extract_memory(self, user_text: str, response: str) -> None:
        try:
            turns = [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": response},
            ]
            extracted = await extract_from_conversation(turns)
            await save_extraction(extracted, self.session.session_id, self.graph)
        except Exception as exc:
            logger.warning(f"[{self.device_id}] Memory extraction failed: {exc}")

    # ── TTS pipeline ─────────────────────────────────────────────

    async def _run_tts_pipeline(
        self,
        queue: asyncio.Queue[str | None],
        soul: SoulState | None,
    ) -> None:
        """
        Concurrent TTS pipeline with 1-sentence lookahead.

        Architecture (eliminates sentence-gap pauses):

          text_queue ──► [_tts_generator task]
                                │
                         gen_out (channel queue, maxsize=3)
                         Each item = per-sentence asyncio.Queue of PCM chunks
                                │
                         [main loop: stream channel → device]

        The generator always starts sentence N+1's TTS generation immediately
        after creating sentence N's channel, so:
          - Sentence N is streaming to device
          - Sentence N+1's TTS HTTP request is already in flight
          - When N finishes, N+1's first chunks are already arriving → gap ≈ 0

        True streaming TTS (httpx.stream):
          TTFA ~300-800ms instead of 3-8s (no wait for full TTS response).
          PCM chunks are yielded from synthesize_stream as they arrive over HTTP.

        Single audio_end: sent once after all sentences complete.
        Firmware streaming_task plays continuously from ring buffer.
        """
        p = soul.pleasure if soul else 0.0
        a = soul.arousal if soul else 0.0
        # 2048B PCM → JSON ~2.8KB（< 4KB ESP32-C3 WS_RECV_BUF，安全）。
        # 每秒 16 chunks（而非 32），ESP32 单核 cJSON+base64 解码负担减半
        # （~22%→~11%），避免 I2S DMA 120ms 缓冲下溢。Pacing 也需对应调 64ms/chunk。
        SEND_CHUNK = 2048
        t_start = time.monotonic()
        total_bytes = 0
        first_audio = True
        sentence_count = 0

        # per-sentence PCM channel: TTS generator writes chunks here,
        # main loop reads and sends to device
        # None sentinel signals end of sentence's audio stream
        SentenceChannel = asyncio.Queue   # type alias for clarity

        # gen_out holds per-sentence channels; maxsize=2 means generator runs
        # 1 sentence ahead of the streamer → prefetch without Doubao throttling
        gen_out: asyncio.Queue[SentenceChannel | None] = asyncio.Queue(maxsize=2)
        gen_tasks: list[asyncio.Task] = []

        async def _tts_to_channel(text: str, ch: asyncio.Queue) -> None:
            """Run TTS for one sentence, pushing PCM chunks into ch."""
            try:
                async for raw in synthesize_stream(text, pad_pleasure=p, pad_arousal=a):
                    await ch.put(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[{self.device_id}] TTS gen error: {exc}")
            finally:
                await ch.put(None)  # sentinel: end of this sentence's audio

        async def _tts_generator() -> None:
            """Consume sentence queue, create TTS tasks + channels, publish to gen_out."""
            while True:
                sentence = await queue.get()
                if sentence is None:
                    await gen_out.put(None)   # propagate sentinel
                    break
                # Create channel, start TTS task, publish channel immediately.
                # The next iteration starts sentence N+1's TTS before the caller
                # has finished sending sentence N to the device.
                ch: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
                task = asyncio.create_task(_tts_to_channel(sentence, ch))
                gen_tasks.append(task)
                logger.debug(f"[{self.device_id}] TTS gen started: '{sentence[:50]}'")
                await gen_out.put(ch)   # may block if gen_out full (back-pressure)

        try:
            gen_task = asyncio.create_task(_tts_generator())

            # ── Pacing: avoid ESP32-C3 ring buffer overflow ──
            # ring buffer 48KB (~1.5s @16kHz mono), TTS 生成速度远大于 I2S 消耗，
            # 不节流会把整段音频一次性推出，超出 ring buffer 部分被丢弃 → 卡顿。
            # 按音频真实时长的 1.2× 发送（发送速度略快于播放，保持缓冲）。
            # 注意 pace_t0 在首次实际 send 时才初始化（TTS 生成耗时不算进 pace 窗口）。
            pace_t0 = None
            audio_ms_sent = 0.0

            while True:
                ch = await gen_out.get()
                if ch is None:
                    break   # all sentences processed

                sentence_count += 1
                buf = bytearray()
                sentence_bytes = 0

                # Stream this sentence's PCM chunks to device as they arrive
                while True:
                    chunk = await ch.get()
                    if chunk is None:
                        break   # end of this sentence
                    buf.extend(chunk)
                    while len(buf) >= SEND_CHUNK:
                        if pace_t0 is None:
                            pace_t0 = time.monotonic()
                        await self.manager.send_audio_to_device(
                            self.device_id, bytes(buf[:SEND_CHUNK])
                        )
                        if first_audio:
                            logger.info(
                                f"[{self.device_id}] TTS first audio: "
                                f"{time.monotonic() - t_start:.2f}s"
                            )
                            first_audio = False
                        total_bytes += SEND_CHUNK
                        sentence_bytes += SEND_CHUNK
                        buf = buf[SEND_CHUNK:]
                        # Pacing: SEND_CHUNK bytes / 32 bytes_per_ms = 真实音频毫秒数
                        audio_ms_sent += SEND_CHUNK / 32.0
                        target_s = audio_ms_sent / 1200   # /1000 * 1/1.2
                        elapsed_s = time.monotonic() - pace_t0
                        if target_s - elapsed_s > 0.005:
                            await asyncio.sleep(target_s - elapsed_s)

                # Flush tail bytes for this sentence
                if buf:
                    await self.manager.send_audio_to_device(self.device_id, bytes(buf))
                    total_bytes += len(buf)
                    sentence_bytes += len(buf)

                logger.debug(
                    f"[{self.device_id}] TTS sentence {sentence_count}: {sentence_bytes}B"
                )

            await gen_task   # ensure generator coroutine is done

            # Single audio_end after the entire response stream.
            # Firmware streaming_task drains remaining ring-buffer then exits.
            await self.manager.send_to_device(
                self.device_id, {"type": "audio_end"}, queue_if_offline=True
            )

            if total_bytes == 0:
                cfg = get_config()
                logger.error(
                    f"[{self.device_id}] TTS returned EMPTY audio! "
                    f"provider={cfg.TTS_PROVIDER}"
                )

            mark_tts_completed(self.device_id)
            logger.info(
                f"[{self.device_id}] TTS pipeline done: {sentence_count} sentences, "
                f"{total_bytes}B, {time.monotonic() - t_start:.2f}s"
            )

        except asyncio.CancelledError:
            # Barge-in: cancel all in-flight TTS tasks, do NOT send audio_end
            gen_task.cancel()
            for t in gen_tasks:
                if not t.done():
                    t.cancel()
            logger.info(
                f"[{self.device_id}] TTS pipeline cancelled (barge-in), "
                f"sent {total_bytes}B"
            )
            _tts_completed_at.pop(self.device_id, None)
            raise

    async def _tts(self, text: str, soul: SoulState | None = None) -> bytes:
        try:
            p = soul.pleasure if soul else 0.0
            a = soul.arousal if soul else 0.0
            return await synthesize(text, pad_pleasure=p, pad_arousal=a)
        except Exception as exc:
            logger.warning(f"[{self.device_id}] TTS error: {exc}")
            return b""

    async def _fallback(self, message: str) -> None:
        await self.manager.broadcast_to_desktops(self.device_id, {
            "type": "response",
            "text": message,
        })
        audio = await self._tts(message)
        if audio:
            await self.manager.send_audio_to_device(self.device_id, audio)
            await self.manager.send_to_device(self.device_id, {"type": "audio_end"})

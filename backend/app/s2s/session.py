"""
S2S 会话管理器

职责：
1. 为每台设备创建并维护一个 DoubaoRealtimeClient 会话
2. 会话建立时从记忆图谱检索热节点，注入到 system_role
3. 将设备侧 binary PCM 转发给 Doubao
4. 翻译 Doubao 事件 → 设备 STATE 命令 + TTS PCM 回传
5. 对话结束后异步触发记忆抽取并存入图谱

Doubao 事件 → 设备状态映射：
  ConnectionStarted(50)  → STATE=LISTENING（开始接收音频）
  ASREnded(459)          → STATE=THINKING（说话结束，等 LLM）
  TTSResponse(352, 首帧) → STATE=SPEAKING（开始播放）+ 转发 PCM
  TTSEnded(359)          → STATE=LISTENING（播完，继续监听）
  ChatEnded(559)         → STATE=LISTENING + 触发记忆抽取
"""
from __future__ import annotations

import asyncio
import json as _json
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

from app.config import get_config
from app.memory.graph import MemoryGraph
from app.s2s.doubao import (
    DoubaoRealtimeClient,
    DoubaoSessionConfig,
    EventReceive,
)
from app.ws.protocol import DeviceState

# 回调类型
AudioCallback = Callable[[bytes], Awaitable[None]]
StateCallback = Callable[[DeviceState], Awaitable[None]]
ErrorCallback = Callable[[str], Awaitable[None]]

_MAX_RECONNECT  = 3
_RECONNECT_WAIT = 3.0
_MEMORY_TOP_K   = 5


async def _build_system_role(device_id: str) -> str:
    """
    从记忆图谱检索 top-K 热节点（按 effective_heat 排序），
    构建注入 Doubao 的 system_role。检索失败时返回默认提示语。
    """
    base_prompt = (
        "你是 Echo，一个贴心的语音助手。"
        "用简洁自然的中文回答，语气友好。"
        "不要重复用户的问题，直接给出有用的回答。"
    )
    try:
        from app.memory.retriever import get_hot_context
        graph = MemoryGraph(device_id)
        context = await get_hot_context(graph, top_k=_MEMORY_TOP_K)
        if not context:
            return base_prompt
        return f"{base_prompt}\n\n关于用户的已知信息：\n{context}"
    except Exception as e:
        logger.warning(f"[S2S] 记忆检索失败 device={device_id}: {e}")
        return base_prompt


async def _trigger_memory_extraction(
    device_id: str,
    turns: list[dict],
) -> None:
    """
    对话结束后异步抽取记忆节点并写入图谱。
    此函数作为后台任务运行，不阻塞对话主路径。
    """
    if not turns:
        return
    try:
        from app.memory.extractor import extract_from_conversation
        result = await extract_from_conversation(turns)
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])
        if not nodes and not edges:
            return
        graph = MemoryGraph(device_id)
        for node in nodes:
            await graph.upsert_node(
                name=node.get("name", ""),
                dimension=node.get("dimension", "knowledge"),
                description=node.get("description", ""),
                aliases=node.get("aliases"),
            )
        for edge in edges:
            await graph.upsert_edge(
                from_name=edge.get("from", ""),
                to_name=edge.get("to", ""),
                relation=edge.get("relation", ""),
            )
        logger.info(
            f"[S2S] 记忆抽取完成 device={device_id}: "
            f"{len(nodes)} 节点, {len(edges)} 边"
        )
    except Exception as e:
        logger.warning(f"[S2S] 记忆抽取失败 device={device_id}: {e}")


@dataclass
class S2SSession:
    """
    单设备的 S2S 会话。

    生命周期：
        session = await S2SSession.create(device_id, on_audio, on_state, on_error)
        await session.send_audio(pcm)
        await session.cancel()
        await session.close()
    """
    device_id: str
    on_audio: AudioCallback    # Doubao TTS PCM → 转发给设备
    on_state: StateCallback    # Doubao 事件 → 设备状态命令
    on_error: ErrorCallback    # 错误通知

    _client: DoubaoRealtimeClient | None = field(default=None, init=False)
    _recv_task: asyncio.Task | None      = field(default=None, init=False)
    _keepalive_task: asyncio.Task | None = field(default=None, init=False)
    _closed: bool                        = field(default=False, init=False)
    _reconnect_count: int                = field(default=0, init=False)
    _current_turns: list[dict]           = field(default_factory=list, init=False)
    _tts_started: bool                   = field(default=False, init=False)

    @classmethod
    async def create(
        cls,
        device_id: str,
        on_audio: AudioCallback,
        on_state: StateCallback,
        on_error: ErrorCallback,
    ) -> "S2SSession":
        session = cls(
            device_id=device_id,
            on_audio=on_audio,
            on_state=on_state,
            on_error=on_error,
        )
        await session._connect()
        return session

    async def _connect(self) -> None:
        cfg = get_config()
        system_role = await _build_system_role(self.device_id)
        doubao_cfg = DoubaoSessionConfig(
            app_id             = cfg.DOUBAO_REALTIME_APP_ID,
            access_key         = cfg.DOUBAO_REALTIME_ACCESS_KEY,
            system_role        = system_role,
            voice_type         = cfg.DOUBAO_REALTIME_VOICE_TYPE,
            output_sample_rate = cfg.DOUBAO_REALTIME_OUTPUT_SAMPLE_RATE,
        )
        self._client = DoubaoRealtimeClient(doubao_cfg)
        await self._client.connect()
        self._recv_task = asyncio.create_task(
            self._event_loop(), name=f"s2s-events-{self.device_id}"
        )
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name=f"s2s-keepalive-{self.device_id}"
        )
        # 会话建立即通知设备开始发送音频
        await self.on_state(DeviceState.LISTENING)
        logger.info(f"[S2S] 会话已建立 device={self.device_id}")

    async def _event_loop(self) -> None:
        """
        Doubao 事件翻译器：将 Doubao 协议事件映射为设备 STATE 命令，
        同时收集对话 turns 以备记忆抽取。
        """
        if self._client is None:
            return
        try:
            async for frm in self._client.events():
                if self._closed:
                    break
                eid = frm.event_id

                # ── TTS 音频帧：转发给设备，首帧触发 SPEAKING 状态 ──────────
                if eid == EventReceive.TTSResponse:
                    if frm.payload:
                        if not self._tts_started:
                            self._tts_started = True
                            await self.on_state(DeviceState.SPEAKING)
                            logger.info(
                                f"[S2S] TTS 首帧 → STATE=SPEAKING device={self.device_id}"
                            )
                        # Doubao 实际返回立体声 PCM（L=语音主声道，R=恒定能量信号）。
                        # 经实测：L 声道 energy variability=1.238（语音/静音交替），
                        # R 声道 energy variability=0.070（近恒定，非语音）。
                        # 取偶数样本（L 声道）转为单声道后发送给设备。
                        pcm = frm.payload
                        n = len(pcm) // 2
                        if n >= 2 and n % 2 == 0:
                            stereo = struct.unpack_from(f"<{n}h", pcm)
                            mono = struct.pack(f"<{n // 2}h", *stereo[0::2])
                            pcm = mono
                        await self.on_audio(pcm)
                    else:
                        logger.warning(
                            f"[S2S] TTSResponse payload 为空 device={self.device_id}"
                        )

                # ── TTS 结束：通知设备播完，恢复监听 ─────────────────────────
                elif eid == EventReceive.TTSEnded:
                    self._tts_started = False
                    await self.on_state(DeviceState.LISTENING)
                    logger.info(f"[S2S] TTS 结束 → STATE=LISTENING device={self.device_id}")

                # ── ASR 说话结束：通知设备切到 THINKING ──────────────────────
                elif eid == EventReceive.ASREnded:
                    await self.on_state(DeviceState.THINKING)
                    logger.debug(f"[S2S] ASR 结束 → STATE=THINKING device={self.device_id}")

                # ── ASR 文本结果：收集用户话语 ──────────────────────────────
                elif eid == EventReceive.ASRInfo:
                    if frm.payload:
                        try:
                            data = _json.loads(frm.payload)
                            text = data.get("text") or data.get("result", "")
                            if text:
                                self._current_turns.append(
                                    {"role": "user", "content": text}
                                )
                                logger.debug(f"[S2S] ASR: {text[:60]}")
                        except Exception:
                            pass

                # ── 对话响应文本：收集助手回答 ──────────────────────────────
                elif eid == EventReceive.ChatResponse:
                    if frm.payload:
                        try:
                            data = _json.loads(frm.payload)
                            text = data.get("text") or data.get("content", "")
                            if text:
                                self._current_turns.append(
                                    {"role": "assistant", "content": text}
                                )
                        except Exception:
                            pass

                # ── 对话轮次结束：触发记忆抽取，重置状态 ────────────────────
                elif eid == EventReceive.ChatEnded:
                    turns = list(self._current_turns)
                    self._current_turns.clear()
                    self._tts_started = False
                    await self.on_state(DeviceState.LISTENING)
                    if turns:
                        asyncio.create_task(
                            _trigger_memory_extraction(self.device_id, turns),
                            name=f"memory-extract-{self.device_id}",
                        )
                    logger.info(
                        f"[S2S] 对话结束 device={self.device_id}, "
                        f"turns={len(turns)}, 触发记忆抽取"
                    )

                # ── 错误处理 ─────────────────────────────────────────────────
                elif eid in (
                    EventReceive.SessionFailed,
                    EventReceive.DialogError,
                    EventReceive.ConnectionFailed,
                ):
                    try:
                        msg = _json.loads(frm.payload).get("message", "未知错误")
                    except Exception:
                        msg = frm.payload.decode(errors="replace") if frm.payload else "未知错误"
                    logger.error(f"[S2S] Doubao 错误 event={eid}: {msg}")
                    await self.on_error(msg)
                    if not self._closed:
                        await self._try_reconnect()
                        return

        except Exception as e:
            logger.warning(f"[S2S] event_loop 异常 device={self.device_id}: {e}")
            if not self._closed:
                await self._try_reconnect()
        else:
            if not self._closed:
                logger.warning(f"[S2S] Doubao 连接意外断开，自动重连 device={self.device_id}")
                await self._try_reconnect()

    async def _keepalive_loop(self) -> None:
        """每 200ms 向 Doubao 发送 100ms 静音 PCM，防止 DialogAudioIdleTimeoutError。"""
        _silence = b"\x00\x00" * 1600   # 100ms @ 16kHz mono int16
        while not self._closed:
            await asyncio.sleep(0.2)
            if self._client and self._client.is_ready:
                await self._client.send_audio(_silence)

    async def _try_reconnect(self) -> None:
        if self._reconnect_count >= _MAX_RECONNECT:
            logger.error(f"[S2S] 重连次数耗尽 device={self.device_id}")
            await self.on_error("Doubao 连接失败，已重试 3 次")
            return
        self._reconnect_count += 1
        logger.info(
            f"[S2S] 重连 {self._reconnect_count}/{_MAX_RECONNECT} "
            f"device={self.device_id}，等待 {_RECONNECT_WAIT}s"
        )
        await asyncio.sleep(_RECONNECT_WAIT)
        try:
            await self._connect()
            self._reconnect_count = 0
        except Exception as e:
            logger.error(f"[S2S] 重连失败: {e}")
            await self._try_reconnect()

    async def send_audio(self, pcm: bytes) -> None:
        """转发设备 PCM 音频到 Doubao。"""
        if self._client and self._client.is_ready:
            await self._client.send_audio(pcm)
        else:
            logger.debug(f"[S2S] 丢弃音频帧（Doubao 未就绪） device={self.device_id}")

    async def send_text(self, text: str) -> None:
        """直接向 Doubao 发文字提问，触发 TTS 响应。"""
        if self._client and self._client.is_ready:
            await self._client.send_text(text)
            logger.info(f"[S2S] 文字提问 device={self.device_id}: {text[:60]}")
        else:
            logger.warning(f"[S2S] 文字提问被丢弃（Doubao 未就绪） device={self.device_id}")

    async def cancel(self) -> None:
        """打断当前对话（barge-in）。"""
        if self._client:
            await self._client.cancel()

    async def close(self) -> None:
        """关闭会话，释放资源。"""
        self._closed = True
        for task in (self._recv_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._client:
            await self._client.close()
        self._client = None
        logger.info(f"[S2S] 会话关闭 device={self.device_id}")


# ── 全局会话注册表 ─────────────────────────────────────────────────────────────

_sessions: dict[str, S2SSession] = {}


async def get_or_create_session(
    device_id: str,
    on_audio: AudioCallback,
    on_state: StateCallback,
    on_error: ErrorCallback,
) -> S2SSession:
    """获取或创建设备的 S2S 会话。设备重连时更新回调。"""
    if device_id in _sessions:
        sess = _sessions[device_id]
        sess.on_audio = on_audio
        sess.on_state = on_state
        sess.on_error = on_error
        return sess
    sess = await S2SSession.create(device_id, on_audio, on_state, on_error)
    _sessions[device_id] = sess
    return sess


async def close_session(device_id: str) -> None:
    """关闭并移除设备的 S2S 会话。"""
    if device_id in _sessions:
        sess = _sessions.pop(device_id)
        await sess.close()

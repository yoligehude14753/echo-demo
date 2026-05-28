"""WebSocket 事件 schema：UI 清单式渲染的数据契约。

PR-14 / m5-t3 协议约定：
- 业务事件类型（server→client）：``EventType``（meeting.*/minutes.*/artifact.*/rag.*/chat.*）
- 协议事件类型（双向）：``ProtocolEventType``（server_hello / server_ping / client_hello / client_ping）
- 所有事件都通过 ``EchoEvent`` 序列化，``seq`` 单调递增由服务端分配
- 客户端首条消息建议发 ``client_hello {last_seq: int}``，服务端会从 ``last_seq+1`` 开始 replay
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "meeting.started",
    "meeting.auto_detected",
    "meeting.auto_ended",
    "meeting.state_changed",
    "meeting.segment",
    "meeting.ended",
    "minutes.ready",
    "minutes.failed",
    "artifact.generating",
    "artifact.ready",
    "artifact.failed",
    "rag.query",
    "rag.answer.delta",
    "rag.answer.done",
    "chat.delta",
    "chat.done",
    "tts.suggested",
    "error",
]

ProtocolEventType = Literal[
    "server_hello",  # 服务端连接确认（含 ws 版本 + 当前 max_seq）
    "server_ping",  # 服务端心跳（每 15s）
    "server_resync",  # last_seq 已过期（history 已淘汰），客户端应清缓存重订阅
    "client_hello",  # 客户端连接握手 + last_seq
    "client_ping",  # 客户端心跳（可选，> 30s 没活动时发）
]

EchoEventType = Literal[
    "meeting.started",
    "meeting.auto_detected",
    "meeting.auto_ended",
    "meeting.state_changed",
    "meeting.segment",
    "meeting.ended",
    "minutes.ready",
    "minutes.failed",
    "artifact.generating",
    "artifact.ready",
    "artifact.failed",
    "rag.query",
    "rag.answer.delta",
    "rag.answer.done",
    "chat.delta",
    "tts.suggested",
    "chat.done",
    "error",
    "server_hello",
    "server_ping",
    "server_resync",
    "client_hello",
    "client_ping",
]

WS_PROTOCOL_VERSION = "1.0"
WS_SERVER_PING_INTERVAL_S = 15.0
WS_CLIENT_INACTIVE_TIMEOUT_S = 45.0


class EchoEvent(BaseModel):
    type: EchoEventType
    seq: int = 0
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    meeting_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ClientHello(BaseModel):
    """客户端首条消息：申报已收到的最大 seq，服务端从下一条 replay。

    协议规则：
    - last_seq=0 → 默认全量 replay（最多 replay_buffer 条）
    - last_seq > server_max_seq → 视为客户端记错，全量 replay
    - history 已被淘汰（last_seq < oldest_seq_in_history）→ 服务端先发 server_resync
    """

    type: Literal["client_hello"] = "client_hello"
    last_seq: int = 0
    client_version: str | None = None

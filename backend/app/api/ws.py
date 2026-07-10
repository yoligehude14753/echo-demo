"""WebSocket 事件总线端点（PR-14 / m5-t3）。

协议约定（``schemas/events.py``）：

握手:
  client → server: {"type": "client_hello", "last_seq": int, "client_version": str?}
  server → client: {"type": "server_hello", "seq": 0, "payload": {"max_seq": int, "version": "1.0"}}

心跳:
  server → client 每 15s: {"type": "server_ping", "seq": 0, "payload": {"max_seq": int}}
  client → server 可选: {"type": "client_ping"} 服务端回相同 server_ping

业务事件:
  server → client: EchoEvent（type 走 EventType；seq 递增）

续传:
  - last_seq=0 → 全量 replay (replay_buffer 容量内)
  - last_seq 落入 history → 仅 replay seq > last_seq
  - last_seq < oldest_history_seq → 服务先发 ``server_resync`` 提示客户端清缓存

兼容性:
  - 老客户端发 "ping" 文本：服务回 server_ping 一次（保留旧 hello-less 行为）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.api.deps import get_event_bus
from app.schemas.events import (
    WS_PROTOCOL_VERSION,
    WS_SERVER_PING_INTERVAL_S,
    EchoEvent,
)

router = APIRouter(tags=["ws"])
logger = logging.getLogger(__name__)

_HELLO_TIMEOUT_S = 1.0  # 客户端 1s 内若不发 client_hello 视为老客户端，默认全量 replay


async def _wait_client_hello(websocket: WebSocket) -> tuple[int, str | None]:
    """等待 client_hello，返回 (last_seq, client_version)；超时按 (0, None)。"""
    try:
        msg = await asyncio.wait_for(websocket.receive_text(), timeout=_HELLO_TIMEOUT_S)
    except TimeoutError:
        return 0, None
    except WebSocketDisconnect:
        raise
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        if msg.strip() == "ping":  # 旧客户端
            return 0, "legacy"
        return 0, None
    if data.get("type") != "client_hello":
        return 0, None
    last_seq = int(data.get("last_seq") or 0)
    return max(0, last_seq), data.get("client_version")


@router.websocket("/ws/echo")
async def ws_echo(
    websocket: WebSocket,
    bus: InMemoryEventBus = Depends(get_event_bus),
) -> None:
    await websocket.accept()

    try:
        last_seq, client_version = await _wait_client_hello(websocket)
    except WebSocketDisconnect:
        return

    # Android / TV 公共 demo 客户端不应收到共享 backend 的历史 replay，否则新安装
    # 后会出现别人的会议和转写。client_version 带 no-replay 时，从当前 max_seq
    # 之后开始订阅，只接连接之后的新事件。
    if client_version and "no-replay" in client_version:
        last_seq = bus.max_seq

    # 客户端可能在 hello 到达后立刻 reload/退出；握手响应也要按正常断开处理，
    # 避免 uvicorn 把浏览器的 1001 going-away 打成 ASGI exception。
    try:
        # 客户端 last_seq 比 history 还早，提示重订阅
        if last_seq > 0 and last_seq < bus.oldest_history_seq:
            await websocket.send_text(
                EchoEvent(
                    type="server_resync",
                    payload={
                        "reason": "history expired",
                        "oldest_seq": bus.oldest_history_seq,
                        "max_seq": bus.max_seq,
                        "client_last_seq": last_seq,
                    },
                ).model_dump_json()
            )
            last_seq = 0  # 重新全量

        await websocket.send_text(
            EchoEvent(
                type="server_hello",
                payload={
                    "max_seq": bus.max_seq,
                    "version": WS_PROTOCOL_VERSION,
                    "client_version": client_version,
                },
            ).model_dump_json()
        )
    except (RuntimeError, WebSocketDisconnect):
        return

    async def _sender() -> None:
        async for evt in bus.subscribe(since_seq=last_seq):
            await websocket.send_text(evt.model_dump_json())

    async def _ping_loop() -> None:
        while True:
            await asyncio.sleep(WS_SERVER_PING_INTERVAL_S)
            await websocket.send_text(
                EchoEvent(type="server_ping", payload={"max_seq": bus.max_seq}).model_dump_json()
            )

    async def _receiver() -> None:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            stripped = msg.strip()
            if stripped == "ping":
                await websocket.send_text(json.dumps({"type": "server_ping"}))
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "client_ping":
                await websocket.send_text(
                    EchoEvent(
                        type="server_ping", payload={"max_seq": bus.max_seq}
                    ).model_dump_json()
                )

    tasks = [
        asyncio.create_task(_sender(), name="ws-sender"),
        asyncio.create_task(_ping_loop(), name="ws-ping"),
        asyncio.create_task(_receiver(), name="ws-receiver"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for task in done:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()

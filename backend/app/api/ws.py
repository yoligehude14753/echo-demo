"""WebSocket 事件总线端点。

GET /ws/echo
  - 推送 JSON 事件流（``EchoEvent``）
  - 客户端可发空 ping，服务端原样返；其他消息忽略
  - 断线即清理订阅
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.api.deps import get_event_bus

router = APIRouter(tags=["ws"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/echo")
async def ws_echo(
    websocket: WebSocket,
    bus: InMemoryEventBus = Depends(get_event_bus),
) -> None:
    await websocket.accept()

    async def _sender() -> None:
        async for evt in bus.subscribe():
            await websocket.send_text(evt.model_dump_json())

    async def _receiver() -> None:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            if msg.strip() == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

    tasks = [asyncio.create_task(_sender()), asyncio.create_task(_receiver())]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(RuntimeError):
            await websocket.close()

"""设备管理 API：在线设备列表、配置推送、队列管理。"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from app.ws.manager import manager

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def list_devices():
    """返回当前在线设备 ID 列表。"""
    return {"devices": manager.online_devices()}


@router.post("/{device_id}/set_config")
async def set_device_config(device_id: str, payload: dict):
    """
    通过 WebSocket 向 ESP32 推送配置更新（如 backend_url）。
    ESP32 收到后写入 NVS 并重启以生效。
    payload 示例: {"key": "backend_url", "value": "wss://echo.yoliyoli.uk"}
    """
    key   = payload.get("key", "")
    value = payload.get("value", "")
    if not key or not value:
        raise HTTPException(status_code=422, detail="key 和 value 均为必填项")
    await manager.send_to_device(device_id, {
        "type": "set_config",
        "key": key,
        "value": value,
    })
    return {"sent": True, "device_id": device_id, "key": key}


@router.delete("/{device_id}/queue")
async def clear_device_queue(device_id: str):
    """清空指定设备的待发消息队列（用于调试）。"""
    count = len(manager._pending.pop(device_id, []))
    return {"cleared": count, "device_id": device_id}

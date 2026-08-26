"""用户档案 API：设备绑定、用户信息、个人偏好。"""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import get_db

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    user_name: str = ""
    nickname: str = ""
    bio: str = ""
    preferences: str = "{}"


@router.get("/{device_id}")
async def get_profile(device_id: str):
    """读取设备对应的用户档案。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT device_id, user_name, nickname, bio, preferences, updated_at "
            "FROM device_profiles WHERE device_id=?",
            (device_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return {
            "device_id": device_id,
            "user_name": "",
            "nickname": "",
            "bio": "",
            "preferences": "{}",
            "updated_at": None,
        }
    return dict(row)


@router.post("/{device_id}")
async def save_profile(device_id: str, body: ProfileUpdate):
    """创建或更新用户档案（upsert）。"""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO device_profiles (device_id, user_name, nickname, bio, preferences, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                   user_name=excluded.user_name,
                   nickname=excluded.nickname,
                   bio=excluded.bio,
                   preferences=excluded.preferences,
                   updated_at=excluded.updated_at""",
            (device_id, body.user_name, body.nickname, body.bio, body.preferences, now),
        )
        await conn.commit()
    return {"saved": True, "device_id": device_id, "updated_at": now}

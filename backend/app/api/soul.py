"""灵魂状态 API：PAD 情感向量 + 关系阶段。"""
from __future__ import annotations
from fastapi import APIRouter

router = APIRouter(prefix="/api/soul", tags=["soul"])


@router.get("/{device_id}")
async def soul_state(device_id: str):
    """返回设备当前灵魂状态（PAD + 关系阶段）。"""
    from app.soul.state import SoulState
    soul = await SoulState.load(device_id)
    return {
        "device_id": device_id,
        "pad": {
            "pleasure": soul.pleasure,
            "arousal": soul.arousal,
            "dominance": soul.dominance,
        },
        "relation": {
            "stage": soul.relation.stage,
            "trust": soul.relation.trust,
            "depth_score": soul.relation.depth_score,
            "interaction_count": soul.relation.interaction_count,
        },
        "summary": soul.pad_summary(),
    }

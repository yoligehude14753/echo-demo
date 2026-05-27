"""SpeakerRegistry：跨 session 持久化的全局说话人编号 + 用户可改 label。

业务规则：
- 任何 ambient / meeting 链路在拿到 diarizer 给的 ``speaker_id`` 后，调
  ``registry.label_for(speaker_id, captured_at=...)`` 得到稳定 label
- 首次出现：``"说话人N"`` 自动编号，N = repo.speakers 总数 + 1
- 之后出现：直接返回 repo 里的 label（即使用户改成了 "李雷"）
- ``None`` 输入 → ``"未识别"``

实现：
- 内存 cache 减少重复查表；进程启动时 ``hydrate()`` 把 speakers 表全部读进来
- 写路径串行通过 ``asyncio.Lock``，避免并发分配重复编号
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.ports.repository import RepositoryPort

_UNKNOWN = "未识别"


class SpeakerRegistry:
    def __init__(self, repository: RepositoryPort | None = None) -> None:
        self._repo = repository
        self._labels: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._hydrated = False

    async def hydrate(self) -> None:
        """从 repo 读全部已注册说话人到内存（startup 调一次）。"""
        if self._repo is None:
            self._hydrated = True
            return
        rows = await self._repo.list_speakers()
        async with self._lock:
            for r in rows:
                if r.label:
                    self._labels[r.speaker_id] = r.label
            self._hydrated = True

    async def label_for(
        self,
        speaker_id: str | None,
        *,
        captured_at: datetime,
    ) -> str:
        """返回稳定 label；首次见 speaker_id 时自动分配 ``"说话人N"``。"""
        if speaker_id is None:
            return _UNKNOWN
        # 已 cache → 直接返回（仍要 upsert 更新 last_seen_at + n_samples）
        cached = self._labels.get(speaker_id)
        if cached is not None:
            if self._repo is not None:
                await self._repo.upsert_speaker(speaker_id, captured_at=captured_at)
            return cached

        async with self._lock:
            cached = self._labels.get(speaker_id)
            if cached is not None:
                return cached
            new_label = await self._allocate_label_unlocked(speaker_id)
            self._labels[speaker_id] = new_label

        if self._repo is not None:
            await self._repo.upsert_speaker(
                speaker_id,
                captured_at=captured_at,
                label=new_label,
            )
        return new_label

    async def _allocate_label_unlocked(self, speaker_id: str) -> str:
        # 数字编号 N = 当前 cache 中 "说话人N" 的最大值 + 1
        # 用户改名的 cache 项不参与编号计算
        max_n = 0
        for label in self._labels.values():
            if label.startswith("说话人"):
                try:
                    n = int(label[len("说话人") :])
                    if n > max_n:
                        max_n = n
                except ValueError:
                    continue
        # 启动时若没 hydrate，第一次分配前补一下（避免重启后从 1 重新开始）
        if not self._hydrated and self._repo is not None:
            existing = await self._repo.list_speakers()
            for r in existing:
                if r.label and r.label.startswith("说话人"):
                    try:
                        n = int(r.label[len("说话人") :])
                        if n > max_n:
                            max_n = n
                    except ValueError:
                        continue
                if r.label:
                    self._labels.setdefault(r.speaker_id, r.label)
            self._hydrated = True
            # hydrate 之后可能 speaker_id 已经在表里了
            if speaker_id in self._labels:
                return self._labels[speaker_id]
        return f"说话人{max_n + 1}"

    async def rename(self, speaker_id: str, new_label: str) -> None:
        """用户手动改名（保留接口给将来的设置 UI 用）。"""
        async with self._lock:
            self._labels[speaker_id] = new_label
        if self._repo is not None:
            await self._repo.upsert_speaker(
                speaker_id, captured_at=datetime.utcnow(), label=new_label
            )

    def known_speaker_ids(self) -> set[str]:
        return set(self._labels.keys())


__all__ = ["SpeakerRegistry"]

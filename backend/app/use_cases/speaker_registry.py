"""SpeakerRegistry：说话人编号 + 用户可改 label。

phase4-speaker-reset（2026-05-28）：把"全局递增编号 + 跨会议持久化"改成
"每个会议独立 counter（从 1 开始）"，由 settings.diarizer_persist_speakers 切换：

- ``diarizer_persist_speakers=False``（默认，本次 PR 引入）：
  * label_for 接受 ``meeting_id``（在线 meeting 时是 meeting_state.current_id；
    否则 ambient sentinel ``__ambient__``）
  * 每个 meeting_id 维护独立 ``{speaker_id: label}`` 映射
  * 同 meeting 内首见 speaker_id → "说话人N"，N = 该 meeting 已分配数 + 1
  * 不调 ``hydrate``、不读/写 ``speakers`` 表（embedding 仅内存里用）
  * 跨进程重启 → counter 全部清零

- ``diarizer_persist_speakers=True``（legacy，env override DIARIZER_PERSIST_SPEAKERS=true）：
  * 老路径：跨 session 持久化的全局编号 + 用户改名
  * 首次出现 ``"说话人N"`` 自动编号，N = repo.speakers 总数 + 1
  * 启动 ``hydrate()`` 把 speakers 表读进来；之后出现直接返回 cached label

业务规则不变：
- ``None`` 输入 → ``"未识别"``
- 用户调用 ``rename`` 改名后，后续 ``label_for`` 返回新名

实现：
- 写路径串行通过 ``asyncio.Lock``，避免并发分配重复编号
- legacy 路径下用全局 ``_labels``；新路径用 ``_per_meeting_labels`` 嵌套字典
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import Settings
from app.ports.repository import RepositoryPort

_UNKNOWN = "未识别"

# meeting_id 为 None 时的默认 context（所有非会议 ambient chunk 共享同一池）。
# 命名与 ECAPA `_AMBIENT_CONTEXT` 对齐，方便日志/排查时关联。
_AMBIENT_CONTEXT = "__ambient__"


class SpeakerRegistry:
    def __init__(
        self,
        repository: RepositoryPort | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings
        # legacy（persist=True）路径：全局 speaker_id → label
        self._labels: dict[str, str] = {}
        # per-meeting（persist=False，新默认）路径：meeting_id → {speaker_id → label}
        # legacy 路径下永远空字典，节省内存。
        self._per_meeting_labels: dict[str, dict[str, str]] = {}
        # 用户 2026-05-28 强诉求：user 手动改过的 label 跨进程持久化。
        # 这层 cache 独立于 persist 设置，hydrate 时只加载 label 非空的 speaker。
        # 命中后 _label_for_per_meeting / legacy 路径都用它，不再分配「说话人 N」。
        self._user_labels: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._hydrated = False

    @property
    def _persist(self) -> bool:
        """settings 未注入 → 默认 False（新行为，对齐 phase4-speaker-reset PR）。

        legacy 测试通过 ``SpeakerRegistry(repo, settings=Settings(diarizer_persist_speakers=True))``
        显式走老路径。
        """
        return bool(self._settings is not None and self._settings.diarizer_persist_speakers)

    async def hydrate(self) -> None:
        """从 repo 读已注册说话人到内存（startup 调一次）。

        - persist=True：全量读 ``label`` 字段进 ``_labels``（legacy 行为）
        - persist=False（默认）：**只**读 ``label_user_set=True`` 的行进
          ``_user_labels``（用户手动改过名的）。自动分配的「说话人 N」不加载，
          避免 ambient 跨进程编号累积；用户改过名的人下次说话能被识别成
          用户起的名字（migration 005 引入 ``label_user_set``）。
        """
        if self._repo is None:
            self._hydrated = True
            return
        rows = await self._repo.list_speakers()
        async with self._lock:
            for r in rows:
                if self._persist:
                    if r.label:
                        self._labels[r.speaker_id] = r.label
                elif r.label and r.label_user_set:
                    self._user_labels[r.speaker_id] = r.label
            self._hydrated = True

    async def label_for(
        self,
        speaker_id: str | None,
        *,
        captured_at: datetime,
        meeting_id: str | None = None,
    ) -> str:
        """返回稳定 label。

        - speaker_id=None → ``"未识别"``
        - persist=False → 在 ``meeting_id`` 范围内分配 ``"说话人N"``，N=该 meeting
          已分配数 + 1；不写 repo、不读 repo（embedding 仅内存）
        - persist=True（legacy） → 跨 meeting 全局编号，写入 ``speakers`` 表
        """
        if speaker_id is None:
            return _UNKNOWN
        if not self._persist:
            return await self._label_for_per_meeting(speaker_id, meeting_id)
        # legacy 路径：cache hit 直接返回（仍 upsert 更新 last_seen_at + n_samples）
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

    async def _label_for_per_meeting(
        self,
        speaker_id: str,
        meeting_id: str | None,
    ) -> str:
        """新路径：在 meeting 内独立分配编号。

        优先命中 ``_user_labels``（用户改过名的人 hydrate / rename 即写入），
        让历史熟人在新会议第一次说话就显示用户起的名字而不是「说话人 N」。
        """
        ctx_key = meeting_id or _AMBIENT_CONTEXT
        async with self._lock:
            mapping = self._per_meeting_labels.setdefault(ctx_key, {})
            cached = mapping.get(speaker_id)
            if cached is not None:
                return cached
            user_label = self._user_labels.get(speaker_id)
            if user_label is not None:
                mapping[speaker_id] = user_label
                return user_label
            # 数字编号 = 当前 mapping 中 "说话人N" 的最大值 + 1
            # 用户改名（rename）的项不参与编号计算（同 legacy 语义）。
            max_n = 0
            for label in mapping.values():
                if label.startswith("说话人"):
                    try:
                        n = int(label[len("说话人") :])
                    except ValueError:
                        continue
                    max_n = max(max_n, n)
            new_label = f"说话人{max_n + 1}"
            mapping[speaker_id] = new_label
            return new_label

    async def _allocate_label_unlocked(self, speaker_id: str) -> str:
        """legacy（persist=True）路径下的全局编号分配，已持 self._lock。"""
        max_n = 0
        for label in self._labels.values():
            if label.startswith("说话人"):
                try:
                    n = int(label[len("说话人") :])
                    max_n = max(max_n, n)
                except ValueError:
                    continue
        # 启动时若没 hydrate，第一次分配前补一下（避免重启后从 1 重新开始）
        if not self._hydrated and self._repo is not None:
            existing = await self._repo.list_speakers()
            for r in existing:
                if r.label and r.label.startswith("说话人"):
                    try:
                        n = int(r.label[len("说话人") :])
                        max_n = max(max_n, n)
                    except ValueError:
                        continue
                if r.label:
                    self._labels.setdefault(r.speaker_id, r.label)
            self._hydrated = True
            if speaker_id in self._labels:
                return self._labels[speaker_id]
        return f"说话人{max_n + 1}"

    async def rename(self, speaker_id: str, new_label: str) -> None:
        """用户手动改名。

        用户 2026-05-28 期望：「用户改过的名称一定要记录下来，下次有相同的声纹
        或者用户改了自己的名称都要改过来」。所以不管 ``persist`` 设置如何，
        user-set label 永远写 repo（强持久化），下次进程启动 hydrate 时能
        匹配回来。

        per-meeting 内存映射也同步：所有曾出现该 ``speaker_id`` 的会议立刻
        换标签，UI 上历史段也同步刷新。
        """
        # 1) 内存：legacy + per-meeting 两条路径都更新
        async with self._lock:
            if self._persist:
                self._labels[speaker_id] = new_label
            else:
                for mapping in self._per_meeting_labels.values():
                    if speaker_id in mapping:
                        mapping[speaker_id] = new_label
            # user-set 优先级最高，记到独立 cache 以便 _label_for_per_meeting
            # 跳过自动编号直接命中（避免重启 hydrate 前的窗口期还是「说话人 N」）
            self._user_labels[speaker_id] = new_label

        # 2) 强持久化：无论 persist 设置都写 repo，并标记 label_user_set=True
        # 让下次 hydrate 能区分"用户改过的" vs "自动分配的「说话人 N」"
        if self._repo is not None:
            await self._repo.upsert_speaker(
                speaker_id,
                captured_at=datetime.now(UTC),
                label=new_label,
                label_user_set=True,
            )

    def user_label_for(self, speaker_id: str) -> str | None:
        """返回 user 手动设置的 label（若有）；纯同步内存读取，供 ECAPA / per-meeting
        路径在分配「说话人 N」前优先命中。"""
        return self._user_labels.get(speaker_id)

    def known_speaker_ids(self) -> set[str]:
        """已知 speaker_id 集合（legacy 路径返全局 keys；新路径返所有 meeting 的并集）。"""
        if not self._persist:
            ids: set[str] = set()
            for mapping in self._per_meeting_labels.values():
                ids.update(mapping.keys())
            return ids
        return set(self._labels.keys())


__all__ = ["SpeakerRegistry"]

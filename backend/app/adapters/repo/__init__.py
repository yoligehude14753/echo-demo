"""Repository adapters：SQLite 主实现 + 工厂函数。"""

from __future__ import annotations

from app.adapters.repo.sqlite import SQLiteRepository
from app.config import Settings
from app.ports.repository import RepositoryPort


def make_repository(settings: Settings) -> RepositoryPort:
    """根据 settings.db_path 创建 SQLite 实现。

    保留未来切换其它后端（pg / 进程内存）的余地，当前固定 SQLite。
    """
    return SQLiteRepository(settings.db_path)


__all__ = ["make_repository", "SQLiteRepository"]

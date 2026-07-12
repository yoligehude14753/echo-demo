"""清库工具：删除 speakers 表（可选 ambient_segments / meeting_speaker_labels）。

为什么单独成工具：
- speaker explosion 经过 spk-1..4 修复后，**新数据**不再爆炸；但 DB 里旧的
  污染数据（spk-1 之前 ECAPA 在每个 6s chunk 上注册新人累积的几百行 speaker_*）
  会被启动 hydrate 时全部 load 回 _profiles，counter 从 max(N) 继续。
- 旧 row embedding_blob 通常是 NULL（spk-1 之前没持久化），即使 hydrate 也是
  noop，但 counter 已经被推到 100+ → UI 看到的"距离 X 个说话人"还是脏数。
- 不能在 backend 启动时自动清，那会丢用户已建立的 speaker 标签；让用户主动
  在"环境干净"时机执行本工具。

使用：
    cd /path/to/echo-demo/backend
    python -m app.tools.reset_speakers           # dry-run，只打印影响行数
    python -m app.tools.reset_speakers --yes     # 实际执行
    python -m app.tools.reset_speakers --yes --include-segments  # 连 ambient_segments 一起清

输出：删除前后的行数对比 + 哪些表被清。
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

from app.adapters.repo.connection import configure_sqlite_connection


def _db_path() -> pathlib.Path:
    """与 backend/app/config.py:Settings.db_path 保持一致。"""
    return pathlib.Path.home() / ".echodesk" / "echodesk.db"


def _count(con: sqlite3.Connection, table: str) -> int:
    row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EchoDesk speakers 表清理工具")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="实际执行（默认 dry-run）",
    )
    parser.add_argument(
        "--include-segments",
        action="store_true",
        help="同时清 ambient_segments + meeting_speaker_labels（保留 meetings 与 meeting_segments）",
    )
    parser.add_argument(
        "--db",
        type=pathlib.Path,
        default=None,
        help="DB 路径（默认 ~/.echodesk/echodesk.db）",
    )
    args = parser.parse_args(argv)

    db = args.db or _db_path()
    if not db.exists():
        print(f"[reset_speakers] DB 不存在：{db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(db)
    configure_sqlite_connection(con)
    try:
        targets: list[str] = ["speakers"]
        if args.include_segments:
            if _exists(con, "ambient_segments"):
                targets.append("ambient_segments")
            if _exists(con, "meeting_speaker_labels"):
                targets.append("meeting_speaker_labels")

        before = {t: _count(con, t) for t in targets}
        print("[reset_speakers] 影响表（删除前行数）:")
        for t, n in before.items():
            print(f"  {t}: {n}")

        if not args.yes:
            print("\n[reset_speakers] dry-run 完成；加 --yes 实际执行。")
            return 0

        for t in targets:
            con.execute(f"DELETE FROM {t}")
        con.commit()

        after = {t: _count(con, t) for t in targets}
        print("\n[reset_speakers] 删除后行数:")
        for t, n in after.items():
            print(f"  {t}: {n}")
        print("\n[reset_speakers] 完成。下次 backend 启动时 ECAPA hydrate 会读到 0 profile。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""一次性清理 sqlite 里 < 10s 的鱼蚂会议（用户测试 manual_start → 立刻 manual_end 残留）。

用户痛点：MeetingList 里看到一长串 0.001-0.04 分钟的 ended 会议，全是噪音。
根因：UI 上误点"开始会议"立刻又点"结束"会留下行；finalize_meeting 路径没处理这种短到没意义的会议。

清理规则（严格）：
- ``state="ended"``（不是 finalized；finalized 的纪要可能用户认真做过）
- ``finalized_at IS NULL``（双重保险）
- ``minutes_json IS NULL``（彻底确认没生成过纪要）
- 持续时间 < 10 秒（duration_threshold_s）

DELETE 是 hard delete；同时 cascade 删 ``transcript_segments`` / ``ambient_segments``（FK ON DELETE CASCADE）。

跑法（dry-run 默认）：
    python scripts/cleanup_short_meetings.py
跑实际删除：
    python scripts/cleanup_short_meetings.py --apply

结果：打印 deleted_count + 留下的行数。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_THRESHOLD_S = 10.0
DEFAULT_DB = Path.home() / ".echodesk" / "echodesk.db"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--threshold-s", type=float, default=DEFAULT_THRESHOLD_S)
    p.add_argument("--apply", action="store_true", help="真的执行 DELETE（默认 dry-run）")
    args = p.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 1

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON;")  # cascade 才能生效
    cur = con.cursor()
    select_sql = """
        SELECT id, started_at, ended_at,
               (julianday(ended_at) - julianday(started_at)) * 86400.0 AS duration_s
          FROM meetings
         WHERE state = 'ended'
           AND finalized_at IS NULL
           AND minutes_json IS NULL
           AND ended_at IS NOT NULL
           AND (julianday(ended_at) - julianday(started_at)) * 86400.0 < ?
    """
    rows = cur.execute(select_sql, (args.threshold_s,)).fetchall()
    print(f"候选鱼蚂会议数: {len(rows)} (threshold={args.threshold_s}s, db={args.db})")
    for r in rows[:10]:
        print(f"  - {r[0]} | started={r[1]} | duration={r[3]:.3f}s")
    if len(rows) > 10:
        print(f"  ... 另 {len(rows) - 10} 条")

    if not args.apply:
        print("\nDRY RUN — 加 --apply 真删")
        return 0

    if not rows:
        print("没东西可删，退出")
        return 0

    ids = tuple(r[0] for r in rows)
    placeholders = ",".join("?" * len(ids))
    cur.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", ids)
    deleted = cur.rowcount
    con.commit()
    con.close()
    print(f"\n已删除 {deleted} 行（cascade 删除关联 segments）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""本地日期时间确定性回答测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from app.use_cases.local_datetime import answer_local_datetime

LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 2, 18, 25, tzinfo=LOCAL)  # Tuesday / 星期二


@pytest.mark.unit
def test_answers_weekday_deterministically() -> None:
    assert answer_local_datetime("今天星期几", now=NOW) == "今天是2026年6月2日，星期二。"
    assert answer_local_datetime("今天周几？", now=NOW) == "今天是2026年6月2日，星期二。"


@pytest.mark.unit
def test_answers_date_and_time_deterministically() -> None:
    assert answer_local_datetime("今天几号", now=NOW) == "今天是2026年6月2日，星期二。"
    assert answer_local_datetime("现在几点", now=NOW) == "现在是2026年6月2日 18:25，星期二。"


@pytest.mark.unit
def test_ignores_non_datetime_questions() -> None:
    assert answer_local_datetime("总结一下今天的会议") is None
    assert answer_local_datetime("查一下最新 AI 新闻") is None

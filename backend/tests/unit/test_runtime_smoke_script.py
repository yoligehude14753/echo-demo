"""runtime_smoke 脚本的轻量单测。"""

from __future__ import annotations

from scripts.stress.runtime_smoke import _sse_events


def test_sse_events_parses_named_json_events() -> None:
    lines = iter(
        [
            "event: delta",
            'data: {"text": "你好"}',
            "",
            "event: final",
            'data: {"answer": "完成"}',
            "",
        ]
    )

    assert list(_sse_events(lines)) == [
        ("delta", {"text": "你好"}),
        ("final", {"answer": "完成"}),
    ]


def test_sse_events_keeps_raw_non_json_data() -> None:
    lines = iter(["event: phase", "data: plain text"])

    assert list(_sse_events(lines)) == [("phase", "plain text")]

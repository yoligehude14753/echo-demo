"""dataclass + JSON 往返测试。"""
from __future__ import annotations

from pathlib import Path

from minutes_kit.models import (
    Decision,
    MeetingMinutesData,
    Todo,
    Topic,
    TranscriptTurn,
)


def test_transcript_turn_round_trip():
    t = TranscriptTurn(speaker="A", text="hello", ts="10:00:00")
    assert TranscriptTurn.from_dict(t.to_dict()) == t


def test_transcript_turn_from_dict_defaults():
    t = TranscriptTurn.from_dict({"text": "hi"})
    assert t.speaker == "?"
    assert t.text == "hi"
    assert t.ts == ""


def test_decision_from_dict_normalizes_empty():
    d = Decision.from_dict({"statement": " hello ", "rationale": "  ", "impact": ""})
    assert d.statement == "hello"
    assert d.rationale is None
    assert d.impact is None


def test_decision_accepts_legacy_key():
    d = Decision.from_dict({"decision": "use Postgres"})
    assert d.statement == "use Postgres"


def test_todo_priority_normalization():
    assert Todo.from_dict({"task": "x", "priority": "H"}).priority == "high"
    assert Todo.from_dict({"task": "x", "priority": "LOW"}).priority == "low"
    assert Todo.from_dict({"task": "x", "priority": "wat"}).priority == "med"
    assert Todo.from_dict({"task": "x"}).priority == "med"


def test_topic_key_points_filters_empty():
    t = Topic.from_dict({"name": "x", "key_points": ["a", "", "  ", "b"]})
    assert t.key_points == ["a", "b"]


def test_topic_key_points_scalar_promoted_to_list():
    t = Topic.from_dict({"name": "x", "key_points": "single"})
    assert t.key_points == ["single"]


def test_minutes_data_round_trip(sample_minutes_data):
    data = sample_minutes_data
    j = data.to_json()
    assert "周三例会" in j

    parsed = MeetingMinutesData.from_dict(data.to_dict())
    assert parsed.minutes_id == data.minutes_id
    assert parsed.title == data.title
    assert len(parsed.decisions) == len(data.decisions)
    assert len(parsed.todos) == len(data.todos)
    assert parsed.todos[0].priority == "high"
    assert parsed.flow_kind == "flowchart"


def test_minutes_data_unknown_flow_kind_fallback():
    d = MeetingMinutesData.from_dict({"flow_kind": "what"})
    assert d.flow_kind == "flowchart"


def test_write_and_read_json(sample_minutes_data, tmp_path: Path):
    p = tmp_path / "data.json"
    sample_minutes_data.write_json(p)
    assert p.exists()
    loaded = MeetingMinutesData.read_json(p)
    assert loaded.title == sample_minutes_data.title
    assert loaded.decisions[0].statement == sample_minutes_data.decisions[0].statement

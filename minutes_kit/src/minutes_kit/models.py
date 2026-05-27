"""核心数据模型：一份正典 JSON 派生 HTML 预览 + docx 导出。

设计要点
--------
- 所有字段都用 `@dataclass`（不引 pydantic 强 schema，保持依赖最小），
  但提供 `to_dict` / `from_dict` 做严格的 JSON 往返
- `MeetingMinutesData` 是唯一真相源：renderer 只读、orchestrator 写
- 时间字段统一存 ISO8601 字符串（避免时区/序列化分歧）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

FlowKind = Literal["flowchart", "sequenceDiagram", "mindmap", "timeline"]
Priority = Literal["high", "med", "low"]


@dataclass(slots=True)
class TranscriptTurn:
    """输入数据：一句转录。"""

    speaker: str
    text: str
    ts: str  # ISO8601 或 "HH:MM:SS"，renderer 不解释只透传

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TranscriptTurn:
        return cls(
            speaker=str(d.get("speaker") or "?"),
            text=str(d.get("text") or ""),
            ts=str(d.get("ts") or ""),
        )


@dataclass(slots=True)
class Decision:
    """一条会议决议。"""

    statement: str
    rationale: str | None = None
    impact: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        return cls(
            statement=str(d.get("statement") or d.get("decision") or "").strip(),
            rationale=_opt_str(d.get("rationale")),
            impact=_opt_str(d.get("impact")),
        )


@dataclass(slots=True)
class Todo:
    """一条待办。"""

    task: str
    owner: str | None = None  # None 表示未指派
    due: str | None = None  # ISO 日期或 "TBD"
    priority: Priority = "med"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Todo:
        pri_raw = str(d.get("priority") or "med").lower().strip()
        if pri_raw in ("high", "h"):
            pri: Priority = "high"
        elif pri_raw in ("low", "l"):
            pri = "low"
        else:
            pri = "med"
        return cls(
            task=str(d.get("task") or "").strip(),
            owner=_opt_str(d.get("owner")),
            due=_opt_str(d.get("due")),
            priority=pri,
        )


@dataclass(slots=True)
class Topic:
    """一个话题段（用于流程图和详情）。"""

    name: str
    time_range: str = ""
    key_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Topic:
        kp_raw = d.get("key_points") or []
        if not isinstance(kp_raw, list):
            kp_raw = [str(kp_raw)]
        return cls(
            name=str(d.get("name") or "").strip(),
            time_range=str(d.get("time_range") or "").strip(),
            key_points=[str(p).strip() for p in kp_raw if str(p).strip()],
        )


@dataclass(slots=True)
class MeetingMinutesData:
    """正典会议纪要数据。所有 renderer 只读这个对象。"""

    minutes_id: str
    title: str
    from_time: str  # ISO
    to_time: str
    participants: list[str]
    abstract: str
    summary_md: str
    decisions: list[Decision] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    flow_mermaid: str = ""
    flow_kind: FlowKind = "flowchart"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_id": self.minutes_id,
            "title": self.title,
            "from_time": self.from_time,
            "to_time": self.to_time,
            "participants": list(self.participants),
            "abstract": self.abstract,
            "summary_md": self.summary_md,
            "decisions": [d.to_dict() for d in self.decisions],
            "todos": [t.to_dict() for t in self.todos],
            "topics": [t.to_dict() for t in self.topics],
            "flow_mermaid": self.flow_mermaid,
            "flow_kind": self.flow_kind,
            "created_at": self.created_at,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeetingMinutesData:
        flow_kind_raw = str(d.get("flow_kind") or "flowchart")
        if flow_kind_raw not in ("flowchart", "sequenceDiagram", "mindmap", "timeline"):
            flow_kind_raw = "flowchart"
        return cls(
            minutes_id=str(d.get("minutes_id") or ""),
            title=str(d.get("title") or "").strip(),
            from_time=str(d.get("from_time") or ""),
            to_time=str(d.get("to_time") or ""),
            participants=[str(p) for p in (d.get("participants") or [])],
            abstract=str(d.get("abstract") or ""),
            summary_md=str(d.get("summary_md") or ""),
            decisions=[Decision.from_dict(x) for x in (d.get("decisions") or [])],
            todos=[Todo.from_dict(x) for x in (d.get("todos") or [])],
            topics=[Topic.from_dict(x) for x in (d.get("topics") or [])],
            flow_mermaid=str(d.get("flow_mermaid") or ""),
            flow_kind=flow_kind_raw,  # type: ignore[arg-type]
            created_at=str(d.get("created_at") or ""),
        )

    @classmethod
    def read_json(cls, path: Path) -> MeetingMinutesData:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass(slots=True)
class MinutesResult:
    """generate_minutes 的返回值：包含数据和产物路径。"""

    data: MeetingMinutesData
    out_dir: Path
    data_json_path: Path
    preview_html_path: Path
    docx_path: Path | None = None  # None = docx 渲染失败
    flow_png_path: Path | None = None  # None = mmdc 未渲染
    docx_generator: str = "unknown"  # "claude" | "python_fallback" | "skipped"
    warnings: list[str] = field(default_factory=list)


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None

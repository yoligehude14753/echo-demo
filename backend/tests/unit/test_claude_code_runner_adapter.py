"""ClaudeCodeRunnerAdapter 单测。"""

from __future__ import annotations

import json

import pytest
from app.agents.claude_code_adapter import ClaudeCodeRunnerAdapter, RunnerEventContext


def _ctx() -> RunnerEventContext:
    return RunnerEventContext(
        task_id="echo_task_1",
        runner_task_id="runner_1",
        conversation_id="conv_1",
        message_id="msg_1",
        title="生成报告",
        agentos_base_url="http://127.0.0.1:4128",
    )


@pytest.mark.unit
def test_adapter_maps_text_tool_artifact_and_result_events() -> None:
    adapter = ClaudeCodeRunnerAdapter()
    ctx = _ctx()

    delta = adapter.translate(
        {"kind": "assistant_text", "task_id": "runner_1", "payload": {"text": "正在分析", "stream": True}},
        context=ctx,
        raw_ref="raw-1",
    )
    assert delta is not None
    assert delta.event == "task.text_delta"
    assert delta.visibility == "user"
    assert delta.text_delta == "正在分析"

    step = adapter.translate(
        {
            "kind": "tool_use",
            "task_id": "runner_1",
            "payload": {"tool_use_id": "toolu_1", "name": "Write", "input": {"file_path": "a.md"}},
        },
        context=ctx,
        raw_ref="raw-2",
    )
    assert step is not None
    assert step.event == "task.step_started"
    assert step.step == {"id": "toolu_1", "label": "正在生成文件", "status": "running"}
    assert "Write" not in json.dumps(step.model_dump(mode="json"), ensure_ascii=False)

    finished = adapter.translate(
        {
            "kind": "tool_result",
            "task_id": "runner_1",
            "payload": {"tool_use_id": "toolu_1", "is_error": False, "output": "ok"},
        },
        context=ctx,
    )
    assert finished is not None
    assert finished.event == "task.step_finished"
    assert finished.step == {"id": "toolu_1", "label": "正在生成文件", "status": "succeeded"}

    artifact = adapter.translate(
        {
            "kind": "artifact_change",
            "task_id": "runner_1",
            "payload": {
                "artifacts": [
                    {
                        "name": "report.docx",
                        "relpath": "out/report.docx",
                        "url": "http://127.0.0.1:4128/api/v1/tasks/runner_1/artifacts/out/report.docx",
                    }
                ]
            },
        },
        context=ctx,
    )
    assert artifact is not None
    assert artifact.event == "task.artifact_updated"
    assert artifact.artifacts[0]["kind"] == "word"
    assert artifact.artifacts[0]["url"].endswith(
        "/agents/tasks/echo_task_1/artifacts/out/report.docx"
    )
    assert "runner_1" not in artifact.artifacts[0]["url"]

    done = adapter.translate(
        {
            "kind": "result",
            "task_id": "runner_1",
            "payload": {"is_error": False, "result_text": "已完成", "duration_ms": 1200},
        },
        context=ctx,
    )
    assert done is not None
    assert done.event == "task.completed"
    assert done.state == "succeeded"
    assert done.message == "已完成"
    assert done.snapshot["duration_ms"] == 1200


@pytest.mark.unit
def test_adapter_keeps_unknown_and_input_delta_debug_only() -> None:
    event = ClaudeCodeRunnerAdapter().translate(
        {"kind": "input_json_delta", "task_id": "runner_1", "payload": {"partial_json": "{}"}},
        context=_ctx(),
    )

    assert event is not None
    assert event.event == "task.runner_status"
    assert event.visibility == "debug"
    assert event.message == "unknown event: input_json_delta"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("task_state", {"status": "failed", "error": "timeout: "}),
        ("result", {"is_error": True, "result_text": "deadline exceeded after 50ms"}),
    ],
)
def test_adapter_promotes_explicit_runner_timeout_to_timeout_event(
    kind: str,
    payload: dict[str, object],
) -> None:
    event = ClaudeCodeRunnerAdapter().translate(
        {"kind": kind, "task_id": "runner_1", "payload": payload},
        context=_ctx(),
    )

    assert event is not None
    assert event.event == "task.timeout"
    assert event.state == "timeout"
    assert event.message

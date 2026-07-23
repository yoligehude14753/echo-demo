from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.agents.agentos import AgentOSBackend
from app.agents.base import AgentIntent
from app.config import Settings


@pytest.mark.unit
def test_public_agentos_runner_requires_an_explicit_endpoint(tmp_path: Path) -> None:
    backend = AgentOSBackend(
        Settings(
            db_path=tmp_path / "public.db",
            storage_dir=tmp_path / "storage",
            public_demo_mode=True,
            agent_os_enabled=True,
            agent_os_url="",
            _env_file=None,  # type: ignore[call-arg]
        )
    )

    result = asyncio.run(
        backend.submit(
            AgentIntent(
                text="执行非内置任务",
                device_id="public-device",
                echo_task_id="echo-task-public",
                runner_operation_key="agent-submit-public",
            )
        )
    )

    assert backend.enabled is False
    assert result.accepted is False
    assert result.error == "public AgentOS runner endpoint is not explicitly configured"


@pytest.mark.unit
def test_public_agentos_submit_keeps_plan_context_and_omits_runner_authority_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "runner-public-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.agents.agentos.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    backend = AgentOSBackend(
        Settings(
            db_path=tmp_path / "public.db",
            storage_dir=tmp_path / "storage",
            public_demo_mode=True,
            agent_os_enabled=True,
            agent_os_url="https://agentos.example",
            llm_main_model="renderer-supplied-model-must-not-cross",
            llm_main_base_url="https://renderer-supplied.invalid/v1",
            _env_file=None,  # type: ignore[call-arg]
        )
    )
    intent = AgentIntent(
        text="请生成执行建议",
        device_id="public-device",
        echo_task_id="echo-task-public",
        runner_operation_key="agent-submit-public",
        runner_model="renderer-model-must-not-cross",
        runner_base_url="https://renderer-base-must-not-cross.invalid",
        context={
            "intent_plan": {
                "execution_target": "claude_code_runtime",
                "steps": ["读取资料", "生成建议"],
            }
        },
    )

    result = asyncio.run(backend.submit(intent))

    assert result.accepted is True
    assert result.runner_task_id == "runner-public-1"
    assert len(seen) == 1
    payload = seen[0]
    assert payload["context"] == intent.context
    assert payload["operation_key"] == intent.runner_operation_key
    assert "runner_model" not in payload
    assert "runner_base_url" not in payload
    assert "credential" not in str(payload).lower()
    assert "grant" not in payload
    assert "limits" not in payload

    asyncio.run(client.aclose())

"""AgentOS client：EchoDesk 后端唯一允许直连 AgentOS 的位置。"""

from __future__ import annotations

import json
import logging

import httpx

from app.agents.base import AgentIntent, AgentSubmitResult
from app.config import Settings

_log = logging.getLogger("echodesk.agents.agentos")


class AgentOSBackend:
    name = "claude_code"

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.agent_os_url.rstrip("/")
        self.enabled = settings.agent_os_enabled
        self._settings = settings

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        if not self.enabled:
            return AgentSubmitResult(
                task_id=intent.echo_task_id or "",
                accepted=False,
                provider=self.name,
                error="agent runner disabled",
            )
        runner_model = intent.runner_model or self._settings.llm_main_model
        runner_base_url = intent.runner_base_url or self._settings.llm_main_base_url
        payload = {
            "text": _compile_runner_prompt(intent),
            "speaker_id": intent.device_id,
            "conversation_id": intent.conversation_id,
            "callback_url": None,
            "priority": intent.priority,
            "timeout_s": intent.timeout_s,
            "reference_file_ids": [],
            "runner_model": runner_model,
            "runner_base_url": runner_base_url,
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                trust_env=False,
            ) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v1/integrations/echo/intent",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            _log.warning("agentos submit failed: %s", exc)
            return AgentSubmitResult(
                task_id=intent.echo_task_id or "",
                accepted=False,
                provider=self.name,
                error=str(exc),
            )

        runner_task_id = str(data.get("id") or data.get("task_id") or "")
        if not runner_task_id:
            return AgentSubmitResult(
                task_id=intent.echo_task_id or "",
                accepted=False,
                provider=self.name,
                error="agent runner response missing task_id",
            )
        return AgentSubmitResult(
            task_id=intent.echo_task_id or runner_task_id,
            accepted=True,
            provider=self.name,
            runner_task_id=runner_task_id,
            runner_base_url=self.base_url,
        )

    async def cancel(self, runner_task_id: str) -> bool:
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.post(f"{self.base_url}/api/v1/tasks/{runner_task_id}/cancel")
            return resp.status_code in (200, 202, 204)
        except httpx.HTTPError as exc:
            _log.warning("agentos cancel failed task=%s: %s", runner_task_id, exc)
            return False


def _compile_runner_prompt(intent: AgentIntent) -> str:
    title = intent.title or "EchoDesk 任务"
    context = json.dumps(intent.context or {}, ensure_ascii=False, indent=2)
    output_contract = json.dumps(intent.output_contract or {}, ensure_ascii=False, indent=2)
    return (
        "你是 EchoDesk 的后台执行 runner。用户只会看到 EchoDesk 任务卡，"
        "不会看到底层 runner、AgentOS 或 provider 名称。\n\n"
        f"任务标题：\n{title}\n\n"
        f"用户原始请求：\n{intent.text}\n\n"
        "执行要求：\n"
        "1. 完成用户请求；必要时读取或修改授权工作区文件、访问网络、运行工具并生成文件。\n"
        "2. 产物必须保存到工作区，完成后用简短中文说明结果。\n"
        "3. 不要在最终说明中提到底层 runner 或 provider。\n\n"
        f"上下文：\n{context}\n\n"
        f"输出要求：\n{output_contract}\n"
    )

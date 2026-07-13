"""AgentOS client：EchoDesk 后端唯一允许直连 AgentOS 的位置。"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx

from app.agents.base import AgentIntent, AgentSubmitResult
from app.config import Settings

_log = logging.getLogger("echodesk.agents.agentos")
AGENTOS_SUBMIT_MAX_WALL_S = 50.0


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
        if not intent.echo_task_id:
            return AgentSubmitResult(
                task_id="",
                accepted=False,
                provider=self.name,
                error="agent submit requires a stable echo task id",
            )
        operation_key = intent.runner_operation_key
        if not operation_key:
            return AgentSubmitResult(
                task_id=intent.echo_task_id,
                accepted=False,
                provider=self.name,
                error="agent submit requires a scoped operation key",
            )
        runner_model = intent.runner_model or self._settings.llm_main_model
        runner_base_url = intent.runner_base_url or self._settings.llm_main_base_url
        payload: dict[str, object] = {
            "operation_key": operation_key,
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
                data: dict[str, object] | None = None
                for attempt in range(2):
                    try:
                        resp = await client.post(
                            f"{self.base_url}/api/v1/integrations/echo/intent",
                            json=payload,
                            headers={"Idempotency-Key": operation_key},
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        break
                    except httpx.HTTPError:
                        if attempt == 1:
                            raise
                if data is None:
                    raise httpx.DecodingError("agent runner response is empty")
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

    async def cancel(self, runner_task_id: str, *, operation_key: str) -> bool:
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                try:
                    resp = await client.post(
                        f"{self.base_url}/api/v1/tasks/{runner_task_id}/cancel",
                        headers={"Idempotency-Key": operation_key},
                    )
                    if resp.status_code in (200, 202, 204):
                        return True
                    if resp.status_code != 409:
                        return False
                except httpx.HTTPError:
                    pass
                state = await client.get(f"{self.base_url}/api/v1/tasks/{runner_task_id}")
                state.raise_for_status()
                return str(state.json().get("status") or "") == "cancelled"
        except httpx.HTTPError as exc:
            _log.warning("agentos cancel failed task=%s: %s", runner_task_id, exc)
            return False

    async def get_task(self, runner_task_id: str) -> dict[str, object] | None:
        """Fetch the authoritative AgentOS task snapshot.

        EchoDesk normally follows the AgentOS WebSocket stream, but the stream is
        intentionally not the only source of truth: if it is unavailable or
        rejects the connection, this HTTP snapshot lets EchoDesk reconcile the
        task state and artifacts instead of leaving the UI stuck in "queued".
        """

        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                trust_env=False,
            ) as client:
                resp = await client.get(f"{self.base_url}/api/v1/tasks/{runner_task_id}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, dict) else None
        except httpx.HTTPError as exc:
            _log.warning("agentos task state fetch failed task=%s: %s", runner_task_id, exc)
            return None


def submit_operation_key(*, tenant_id: str, owner_id: str, task_id: str) -> str:
    material = f"v1\0{tenant_id}\0{owner_id}\0{task_id}\0submit".encode()
    return f"agent-submit-{hashlib.sha256(material).hexdigest()}"


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

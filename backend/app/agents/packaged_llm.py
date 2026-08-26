"""Packaged Agent backend：把已授权任务交给 EchoDesk 共享 LLM 单例。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.adapters.llm.openai_compatible import LLMError
from app.agents.base import AgentIntent, AgentSubmitResult
from app.agents.events import utc_now_iso
from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.llm import ChatMessage


_SYSTEM_PROMPT = """你是 EchoDesk 内置任务助手。请直接完成用户交给你的任务，并返回可展示的最终文本。
当前 packaged 兼容运行只支持模型调用，不执行工具或写文件；不要声称已经创建、修改或保存了文件。"""


class PackagedLLMAgentBackend:
    """Process-local LLM runner with the existing AgentOS-compatible snapshot shape."""

    base_url = "embedded://yoli-llm"
    enabled = True
    provider = "yoli_llm"

    def __init__(self, settings: Settings, llm: LLMPort) -> None:
        self._settings = settings
        self._llm = llm
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._runner_by_operation: dict[str, str] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def submit(self, intent: AgentIntent) -> AgentSubmitResult:
        task_id = (intent.echo_task_id or "").strip()
        operation_key = (intent.runner_operation_key or "").strip()
        if not task_id or not operation_key:
            return AgentSubmitResult(
                task_id=task_id or "unbound",
                accepted=False,
                provider=self.provider,
                error="任务缺少本地运行标识",
                runner_base_url=self.base_url,
            )

        runner_task_id = f"local_llm:{task_id}"
        async with self._lock:
            existing_runner = self._runner_by_operation.get(operation_key)
            if existing_runner is not None:
                return self._accepted(task_id, existing_runner)
            if runner_task_id in self._snapshots:
                self._runner_by_operation[operation_key] = runner_task_id
                return self._accepted(task_id, runner_task_id)

            self._runner_by_operation[operation_key] = runner_task_id
            self._snapshots[runner_task_id] = {
                "id": runner_task_id,
                "runner_task_id": runner_task_id,
                "status": "running",
                "final_text": None,
                "artifacts": [],
                "started_at": utc_now_iso(),
            }
            self._jobs[runner_task_id] = asyncio.create_task(
                self._run(runner_task_id, intent),
                name=f"packaged-llm-agent:{task_id}",
            )
        return self._accepted(task_id, runner_task_id)

    async def cancel(self, runner_task_id: str, *, operation_key: str) -> bool:
        del operation_key
        job: asyncio.Task[None] | None = None
        async with self._lock:
            snapshot = self._snapshots.get(runner_task_id)
            if snapshot is None:
                return False
            if str(snapshot.get("status")) in {
                "succeeded",
                "failed",
                "timeout",
                "cancelled",
            }:
                return True
            snapshot.update(
                status="cancelled",
                final_text="任务已取消",
                finished_at=utc_now_iso(),
            )
            job = self._jobs.get(runner_task_id)
        if job is not None and not job.done():
            job.cancel()
        return True

    async def get_task(self, runner_task_id: str) -> dict[str, object] | None:
        async with self._lock:
            snapshot = self._snapshots.get(runner_task_id)
            if snapshot is not None:
                return dict(snapshot)
        if runner_task_id.startswith("local_llm:"):
            return {
                "id": runner_task_id,
                "runner_task_id": runner_task_id,
                "status": "failed",
                "error": "LOCAL_RUNNER_INTERRUPTED",
                "final_text": "任务在服务重启时中断，请重新执行",
                "artifacts": [],
                "finished_at": utc_now_iso(),
            }
        return None

    async def aclose(self) -> None:
        async with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def _accepted(self, task_id: str, runner_task_id: str) -> AgentSubmitResult:
        return AgentSubmitResult(
            task_id=task_id,
            accepted=True,
            provider=self.provider,
            runner_task_id=runner_task_id,
            runner_base_url=self.base_url,
        )

    async def _run(self, runner_task_id: str, intent: AgentIntent) -> None:
        started = time.monotonic()
        context = {
            "task": intent.text,
            "context": intent.context,
            "output_contract": intent.output_contract,
        }
        try:
            response = await self._llm.chat(
                [
                    ChatMessage(role="system", content=_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=json.dumps(context, ensure_ascii=False, default=str),
                    ),
                ],
                model=self._settings.llm_main_model,
                max_tokens=self._settings.llm_main_max_tokens,
                temperature=0.2,
                timeout_s=min(intent.timeout_s, self._settings.model_gateway_timeout_s),
                priority="background",
            )
            await self._finish(
                runner_task_id,
                status="succeeded",
                final_text=response.content.strip() or "任务完成",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                model=response.model,
            )
        except asyncio.CancelledError:
            raise
        except LLMError as error:
            status = "timeout" if error.category == "timeout" else "failed"
            await self._finish(
                runner_task_id,
                status=status,
                final_text="模型调用超时" if status == "timeout" else "模型调用失败",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                model=error.resolved_model,
                error="MODEL_TIMEOUT" if status == "timeout" else "MODEL_REQUEST_FAILED",
            )
        except TimeoutError:
            await self._finish(
                runner_task_id,
                status="timeout",
                final_text="模型调用超时",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                error="MODEL_TIMEOUT",
            )
        except Exception:
            await self._finish(
                runner_task_id,
                status="failed",
                final_text="模型调用失败",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                error="MODEL_REQUEST_FAILED",
            )

    async def _finish(
        self,
        runner_task_id: str,
        *,
        status: str,
        final_text: str,
        duration_ms: float,
        model: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            snapshot = self._snapshots.get(runner_task_id)
            if snapshot is None or snapshot.get("status") == "cancelled":
                return
            snapshot.update(
                status=status,
                final_text=final_text,
                artifacts=[],
                duration_ms=duration_ms,
                finished_at=utc_now_iso(),
            )
            if model:
                snapshot["model"] = model
            if error:
                snapshot["error"] = error


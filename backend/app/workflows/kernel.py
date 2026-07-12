from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeAlias
from uuid import uuid4

from app.runtime.execution_lease import LeaseOwnershipError, LeaseToken
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import bind_principal, current_principal, reset_principal
from app.workflows.service import (
    WorkflowRunRecord,
    WorkflowService,
    bind_workflow_execution_lease,
    reset_workflow_execution_lease,
)


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    run_id: str
    attempt: int
    cancel_event: asyncio.Event
    fence_token: int = 0


WorkflowHandler: TypeAlias = Callable[
    [WorkflowContext, dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]
]
WorkflowScopePreparer: TypeAlias = Callable[[], None]


class WorkflowScopeLease(Protocol):
    def release(self) -> None: ...


WorkflowScopeLeaseFactory: TypeAlias = Callable[[tuple[str, str]], WorkflowScopeLease]

log = logging.getLogger("echodesk.workflow.kernel")


class WorkflowExecutionError(RuntimeError):
    """A dispatched workflow did not reach ``succeeded``.

    API adapters can translate this single application error into their own
    transport status without reimplementing dispatcher lifecycle checks.
    """

    def __init__(self, run_id: str, run: WorkflowRunRecord | None) -> None:
        self.run_id = run_id
        self.run = run
        self.state = run.state if run is not None else "missing"
        message = run.error if run is not None and run.error else f"workflow ended in {self.state}"
        super().__init__(message)


class WorkflowHandlerRegistry:
    def __init__(self, *, max_scopes: int = 256) -> None:
        if max_scopes < 1:
            raise ValueError("workflow handler scope bound must be positive")
        self.max_scopes = max_scopes
        self._handlers: dict[tuple[str, str | None, str | None], WorkflowHandler] = {}
        self._scopes: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _touch_scope(self, scope: tuple[str, str]) -> None:
        if scope in self._scopes:
            self._scopes.move_to_end(scope)
            return
        if len(self._scopes) >= self.max_scopes:
            evicted, _ = self._scopes.popitem(last=False)
            self._drop_scope_handlers(evicted)
        self._scopes[scope] = None

    def _drop_scope_handlers(self, scope: tuple[str, str]) -> None:
        for key in [key for key in self._handlers if key[1:] == scope]:
            self._handlers.pop(key, None)

    def register(
        self,
        kind: str,
        handler: WorkflowHandler,
        *,
        scope: tuple[str, str] | None = None,
        replace: bool = False,
    ) -> None:
        normalized = kind.strip()
        if not normalized:
            raise ValueError("workflow handler kind must not be empty")
        key = (normalized, *(scope or (None, None)))
        if key in self._handlers and not replace:
            raise ValueError(f"workflow handler already registered: {normalized} scope={scope}")
        if scope is not None:
            self._touch_scope(scope)
        self._handlers[key] = handler

    def resolve(
        self,
        kind: str,
        scope: tuple[str, str] | None = None,
    ) -> WorkflowHandler | None:
        if scope is not None:
            scoped = self._handlers.get((kind, *scope))
            if scoped is not None:
                self._scopes.move_to_end(scope)
                return scoped
        return self._handlers.get((kind, None, None))

    def unregister_scope(self, scope: tuple[str, str]) -> None:
        self._drop_scope_handlers(scope)
        self._scopes.pop(scope, None)

    def kinds(self) -> frozenset[str]:
        return frozenset(key[0] for key in self._handlers)

    @property
    def scope_count(self) -> int:
        return len(self._scopes)

    @property
    def handler_count(self) -> int:
        return len(self._handlers)


class WorkflowDispatcher:
    """Execute registered workflow handlers with durable run state and cancellation."""

    def __init__(
        self,
        service: WorkflowService,
        registry: WorkflowHandlerRegistry | None = None,
        *,
        worker_id: str | None = None,
        scope_lease_factory: WorkflowScopeLeaseFactory | None = None,
    ) -> None:
        self.service = service
        self.registry = registry or WorkflowHandlerRegistry(
            max_scopes=service.settings.runtime_scope_max_entries
        )
        self.worker_id = worker_id or (f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}")
        self._scope_lease_factory = scope_lease_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._recovery_reaper_task: asyncio.Task[None] | None = None
        self._prepare_current_scope: WorkflowScopePreparer | None = None

    async def dispatch(self, body: WorkflowRunCreate) -> WorkflowRunRecord:
        run = await self.service.create_run(body)
        await self._ensure_scheduled(run)
        return run

    async def execute(self, body: WorkflowRunCreate) -> WorkflowRunRecord:
        """Dispatch once and return only a durable successful terminal record."""

        run = await self.dispatch(body)
        return await self.wait_succeeded(run.run_id)

    async def wait_succeeded(self, run_id: str) -> WorkflowRunRecord:
        """Wait for an existing run and require a durable successful terminal state."""

        done = await self.wait(run_id)
        if done is None or done.state != "succeeded":
            raise WorkflowExecutionError(run_id, done)
        return done

    async def _ensure_scheduled(
        self,
        run: WorkflowRunRecord,
        *,
        restored: bool = False,
        count_already_scheduled: bool = True,
    ) -> bool:
        async with self._lock:
            existing = self._tasks.get(run.run_id)
            if existing is not None and not existing.done():
                return count_already_scheduled
            claimed = await self.service.claim_run_for_execution(
                run.run_id,
                holder_id=self.worker_id,
            )
            if claimed is None:
                return False
            claimed_run, lease = claimed
            if restored:
                await self.service.record_event(
                    run.run_id,
                    "workflow.restored",
                    message="任务已恢复到 dispatcher",
                    payload={"state": run.state, "attempt": run.attempt},
                    visibility="debug",
                    lease=lease,
                )
            cancel_event = asyncio.Event()
            self._cancel_events[run.run_id] = cancel_event
            principal = current_principal()
            scope = (principal.tenant_id, principal.owner_id)
            handler = self.registry.resolve(claimed_run.kind, scope)
            scope_lease: WorkflowScopeLease | None = None
            try:
                if handler is not None and self._scope_lease_factory is not None:
                    scope_lease = self._scope_lease_factory(scope)
            except BaseException:
                self._cancel_events.pop(run.run_id, None)
                await self.service.release_run_lease(lease)
                raise
            task = asyncio.create_task(
                self._execute(claimed_run, cancel_event, lease, handler, scope_lease),
                name=f"workflow:{claimed_run.kind}:{run.run_id}",
            )
            self._tasks[run.run_id] = task
            task.add_done_callback(self._task_done_callback(run.run_id))
            return True

    def _task_done_callback(self, run_id: str) -> Callable[[asyncio.Task[None]], None]:
        def done(_task: asyncio.Task[None]) -> None:
            self._drop_task(run_id)

        return done

    def _drop_task(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)

    async def _execute(  # noqa: PLR0911, PLR0912, PLR0915 - explicit lifecycle outcomes
        self,
        run: WorkflowRunRecord,
        cancel_event: asyncio.Event,
        lease: LeaseToken,
        handler: WorkflowHandler | None,
        scope_lease: WorkflowScopeLease | None,
    ) -> None:
        lease_context = bind_workflow_execution_lease(lease)
        lease_lost = asyncio.Event()
        handler_task: asyncio.Task[dict[str, Any]] | None = None
        heartbeat_error_observed = False
        heartbeat_task = asyncio.create_task(
            self._heartbeat_lease(run.run_id, lease, cancel_event, lease_lost),
            name=f"workflow-heartbeat:{run.run_id}",
        )
        try:
            if handler is None:
                await self.service.fail_run(
                    run.run_id, error=f"workflow handler not registered: {run.kind}"
                )
                return
            current = await self.service.get_run(run.run_id)
            if current is None or current.is_terminal:
                return
            if current.state == "cancel_requested":
                await self.service.mark_cancelled(run.run_id)
                return
            if current.state == "pending":
                current = await self.service.start_run(run.run_id)
                if current is None:
                    return
            context = WorkflowContext(
                run_id=run.run_id,
                attempt=current.attempt,
                fence_token=lease.fence_token,
                cancel_event=cancel_event,
            )
            timeout_s = self._remaining_timeout(current)
            if timeout_s is not None and timeout_s <= 0:
                raise TimeoutError
            handler_task = asyncio.create_task(
                handler(context, dict(current.input)),
                name=f"workflow-handler:{run.run_id}",
            )
            cancel_wait = asyncio.create_task(cancel_event.wait())
            lease_wait = asyncio.create_task(lease_lost.wait())
            done, _pending = await asyncio.wait(
                {handler_task, cancel_wait, lease_wait, heartbeat_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in (cancel_wait, lease_wait):
                if waiter not in done:
                    waiter.cancel()
            await asyncio.gather(cancel_wait, lease_wait, return_exceptions=True)
            if heartbeat_task in done:
                if heartbeat_task.cancelled():
                    lease_lost.set()
                else:
                    heartbeat_error = heartbeat_task.exception()
                    if heartbeat_error is not None:
                        heartbeat_error_observed = True
                        log.warning(
                            "workflow lease heartbeat failed: run_id=%s worker_id=%s: %s",
                            run.run_id,
                            self.worker_id,
                            heartbeat_error,
                        )
            if lease_wait in done or lease_lost.is_set():
                cancel_event.set()
                await self._stop_handler(handler_task)
                return
            if cancel_wait in done or cancel_event.is_set():
                await self._stop_handler(handler_task)
                await self.service.mark_cancelled(run.run_id)
                return
            if heartbeat_task in done:
                # A normal heartbeat exit means another actor made the run
                # terminal.  Never let the stale handler project afterwards.
                cancel_event.set()
                await self._stop_handler(handler_task)
                return
            if handler_task not in done:
                cancel_event.set()
                await self._stop_handler(handler_task)
                raise TimeoutError
            heartbeat_cancel_requested = heartbeat_task.cancel()
            heartbeat_result = (await asyncio.gather(heartbeat_task, return_exceptions=True))[0]
            if isinstance(heartbeat_result, BaseException) and not isinstance(
                heartbeat_result, asyncio.CancelledError
            ):
                heartbeat_error_observed = True
                lease_lost.set()
                cancel_event.set()
                log.warning(
                    "workflow lease heartbeat failed before completion: run_id=%s worker_id=%s: %s",
                    run.run_id,
                    self.worker_id,
                    heartbeat_result,
                )
                return
            if not heartbeat_cancel_requested or lease_lost.is_set():
                cancel_event.set()
                return
            output = handler_task.result()
            await self.service.complete_run(run.run_id, output=output)
        except LeaseOwnershipError:
            cancel_event.set()
        except TimeoutError:
            cancel_event.set()
            with contextlib.suppress(LeaseOwnershipError):
                await self.service.timeout_run(run.run_id)
        except asyncio.CancelledError:
            cancel_event.set()
            if not self._closing:
                current = await self.service.get_run(run.run_id)
                if current is not None and current.state == "cancel_requested":
                    with contextlib.suppress(LeaseOwnershipError):
                        await self.service.mark_cancelled(run.run_id)
        except Exception as exc:
            with contextlib.suppress(LeaseOwnershipError):
                await self.service.fail_run(run.run_id, error=str(exc))
        finally:
            if handler_task is not None:
                await self._stop_handler(handler_task)
            heartbeat_task.cancel()
            heartbeat_result = (await asyncio.gather(heartbeat_task, return_exceptions=True))[0]
            if (
                not heartbeat_error_observed
                and isinstance(heartbeat_result, BaseException)
                and not isinstance(heartbeat_result, asyncio.CancelledError)
            ):
                log.warning(
                    "workflow lease heartbeat failed during teardown: run_id=%s worker_id=%s: %s",
                    run.run_id,
                    self.worker_id,
                    heartbeat_result,
                )
            reset_workflow_execution_lease(lease_context)
            try:
                await self.service.release_run_lease(lease)
            finally:
                if scope_lease is not None:
                    scope_lease.release()

    async def _heartbeat_lease(
        self,
        run_id: str,
        lease: LeaseToken,
        cancel_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self.service.settings.execution_lease_heartbeat_s)
                renewed = await self.service.renew_run_lease(lease)
                if renewed is None:
                    lease_lost.set()
                    cancel_event.set()
                    return
                current = await self.service.get_run(run_id)
                if current is None or current.is_terminal:
                    return
                if current.state == "cancel_requested":
                    cancel_event.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A renewal error is indistinguishable from losing ownership.  The
            # handler must stop before another worker can acquire a fresh fence.
            cancel_event.set()
            lease_lost.set()
            raise

    async def recover_unfinished_scopes(self) -> int:
        """Claim resumable work across persisted scopes using durable leases."""

        restored = 0
        principals = await self.service.list_unfinished_principals()
        for principal in principals:
            if self._closing:
                break
            principal_token = bind_principal(principal)
            try:
                if self._prepare_current_scope is not None:
                    self._prepare_current_scope()
                restored += await self.restore_unfinished(count_already_scheduled=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "workflow recovery failed for scope tenant=%s owner=%s: %s",
                    principal.tenant_id,
                    principal.owner_id,
                    exc,
                )
            finally:
                reset_principal(principal_token)
        return restored

    def start_recovery_reaper(
        self,
        *,
        prepare_current_scope: WorkflowScopePreparer | None = None,
        interval_s: float | None = None,
        max_interval_s: float | None = None,
    ) -> None:
        """Continuously recover expired unfinished work until dispatcher close.

        Idle scans back off to a lease-derived finite ceiling.  A successful
        claim resets the delay, while SQLite execution leases remain the sole
        authority deciding which process may schedule a run.
        """

        if self._closing:
            raise RuntimeError("cannot start workflow recovery on a closed dispatcher")
        if prepare_current_scope is not None:
            self._prepare_current_scope = prepare_current_scope
        task = self._recovery_reaper_task
        if task is not None and not task.done():
            return

        ttl_s = self.service.settings.execution_lease_ttl_s
        heartbeat_s = self.service.settings.execution_lease_heartbeat_s
        base_delay = interval_s if interval_s is not None else max(0.1, min(heartbeat_s, ttl_s / 3))
        max_delay = (
            max_interval_s if max_interval_s is not None else max(base_delay, min(30.0, ttl_s / 2))
        )
        if not math.isfinite(base_delay) or base_delay <= 0:
            raise ValueError("workflow recovery interval must be finite and positive")
        if not math.isfinite(max_delay) or max_delay < base_delay:
            raise ValueError("workflow recovery max interval must be finite and >= interval")
        self._recovery_reaper_task = asyncio.create_task(
            self._recovery_reaper_loop(
                interval_s=base_delay,
                max_interval_s=max_delay,
            ),
            name=f"workflow-recovery-reaper:{self.worker_id}",
        )

    async def _recovery_reaper_loop(
        self,
        *,
        interval_s: float,
        max_interval_s: float,
    ) -> None:
        delay = interval_s
        while True:
            await asyncio.sleep(delay)
            try:
                restored = await self.recover_unfinished_scopes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive process loop
                log.warning("workflow recovery reaper failed: %s", exc)
                restored = 0
            delay = interval_s if restored else min(max_interval_s, delay * 2)

    @staticmethod
    async def _stop_handler(handler_task: asyncio.Task[dict[str, Any]]) -> None:
        if not handler_task.done():
            handler_task.cancel()
        await asyncio.gather(handler_task, return_exceptions=True)

    @staticmethod
    def _remaining_timeout(run: WorkflowRunRecord) -> float | None:
        """Keep the original absolute deadline when a process restarts."""

        if run.deadline_at:
            try:
                deadline = datetime.fromisoformat(run.deadline_at)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=UTC)
                return (deadline - datetime.now(UTC)).total_seconds()
            except ValueError:
                pass
        return run.timeout_s

    async def cancel(self, run_id: str, *, reason: str | None = None) -> WorkflowRunRecord | None:
        existing = await self.service.get_run(run_id)
        if existing is None or existing.is_terminal:
            return existing
        run = await self.service.request_cancel(run_id, reason=reason)
        if run is None:
            return run
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return run
        await asyncio.gather(task, return_exceptions=True)
        return await self.service.get_run(run_id)

    async def retry(self, run_id: str, *, reason: str | None = None) -> WorkflowRunRecord | None:
        retry = await self.service.retry_run(run_id, reason=reason)
        if retry is not None:
            await self._ensure_scheduled(retry)
        return retry

    async def restore_unfinished(self, *, count_already_scheduled: bool = True) -> int:
        await self.service.drain_outbox()
        runs = await self.service.list_unfinished_runs()
        restored = 0
        for run in runs:
            if run.kind == "agent_task":
                continue
            if count_already_scheduled:
                scheduled = await self._ensure_scheduled(run, restored=True)
            else:
                scheduled = await self._ensure_scheduled(
                    run,
                    restored=True,
                    count_already_scheduled=False,
                )
            if scheduled:
                restored += 1
        return restored

    async def wait(self, run_id: str) -> WorkflowRunRecord | None:
        while True:
            task = self._tasks.get(run_id)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            current = await self.service.get_run(run_id)
            if current is None or current.is_terminal or self._closing:
                return current
            await self._ensure_scheduled(current)
            await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        self._closing = True
        reaper_task = self._recovery_reaper_task
        self._recovery_reaper_task = None
        if reaper_task is not None:
            reaper_task.cancel()
            await asyncio.gather(reaper_task, return_exceptions=True)
        async with self._lock:
            tasks = [task for task in self._tasks.values() if not task.done()]
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()


__all__ = [
    "WorkflowContext",
    "WorkflowDispatcher",
    "WorkflowExecutionError",
    "WorkflowHandler",
    "WorkflowHandlerRegistry",
]

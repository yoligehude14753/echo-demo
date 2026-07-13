"""HTTP API: RAG 入库 + 检索式问答。

POST /rag/ingest        — multipart 上传任意文档入库（PDF/docx/pptx/xlsx/md/txt/csv/...）
POST /rag/ask           — 检索式问答（SSE 流式）
GET  /rag/stats         — 索引诊断
GET  /rag/docs          — 列出所有已入库文档
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.rag import BM25Rag
from app.adapters.web_search import TavilyWebSearch
from app.api.deps import get_llm_singleton as get_llm
from app.api.deps import get_quota_governor, get_workflow_dispatcher
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.web_search import WebSearchPort
from app.schemas.rag import (
    RagAnswerDeltaEvent,
    RagAnswerDoneEvent,
    RagAnswerErrorEvent,
    RagAnswerErrorTrace,
    RagAnswerMeta,
    RagAnswerSource,
    RagAnswerTrace,
    RetrievalResult,
)
from app.schemas.workflow import TERMINAL_WORKFLOW_STATES, WorkflowRunCreate
from app.security.context import current_principal
from app.security.errors import InternalHTTPException
from app.security.governor import PrincipalGovernor
from app.security.public_projection import project_client_dict
from app.upload import UploadTooLarge, read_limited_upload
from app.upload.ownership import (
    bind_rag_content_doc,
    claim_rag_content,
    get_rag_content_claim,
    open_rag_parser_input,
    release_rag_content_claim,
    stage_rag_content_blob,
)
from app.use_cases.retrieve_and_answer import AnswerStream, retrieve_and_answer
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError
from app.workflows.service import WorkflowRunRecord, new_workflow_run_id

router = APIRouter(tags=["rag"])
logger = logging.getLogger("echodesk.retrieval_api")
MAX_RAG_QUESTION_CHARS = 32_000


_rag_singleton: BM25Rag | None = None
_web_singleton: TavilyWebSearch | None = None


def get_rag(settings: Settings = Depends(get_settings)) -> RagPort:
    global _rag_singleton  # noqa: PLW0603
    if _rag_singleton is None:
        _rag_singleton = BM25Rag(settings)
    return _rag_singleton


def get_web(settings: Settings = Depends(get_settings)) -> WebSearchPort:
    global _web_singleton  # noqa: PLW0603
    if _web_singleton is None:
        _web_singleton = TavilyWebSearch(settings)
    return _web_singleton


def reset_singletons() -> None:
    """供测试用。"""
    global _rag_singleton, _web_singleton  # noqa: PLW0603
    _rag_singleton = None
    _web_singleton = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_RAG_QUESTION_CHARS)
    rag_top_k: int = 5
    web_top_n: int = 5


def _resume_workflow_body(run: WorkflowRunRecord) -> WorkflowRunCreate:
    """Recreate the exact command needed to schedule an already-durable run."""

    return WorkflowRunCreate(
        kind=run.kind,
        source=run.source,
        title=run.title,
        intent_text=run.intent_text,
        meeting_id=run.meeting_id,
        todo_id=run.todo_id,
        agent_task_id=run.agent_task_id,
        input=run.input,
        timeout_s=run.timeout_s,
        idempotency_key=run.idempotency_key,
        active_key=run.active_key,
    )


def bind_rag_workflow_handlers(
    dispatcher: WorkflowDispatcher,
    rag: RagPort,
    settings: Settings,
) -> None:
    """Bind durable RAG handlers once; all request data remains JSON-serializable."""

    async def ingest_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        principal = current_principal()
        digest = str(payload["content_hash"])
        title = str(payload.get("title") or "Document")
        async with open_rag_parser_input(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
            workflow_run_id=context.run_id,
        ) as input_path:
            doc_id = await rag.ingest_file(
                str(input_path),
                doc_title=title,
                source=str(payload.get("source") or "upload"),
                source_path=(str(payload["source_path"]) if payload.get("source_path") else None),
                operation_id=context.run_id,
            )
        try:
            await bind_rag_content_doc(
                settings.db_path,
                principal,
                content_hash=digest,
                workflow_run_id=context.run_id,
                doc_id=doc_id,
            )
        except BaseException:
            await rag.delete(doc_id)
            raise
        return {"doc_id": doc_id, "title": title}

    async def delete_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        doc_id = str(payload["doc_id"])
        await rag.delete(doc_id)
        released = await release_rag_content_claim(
            settings.db_path,
            settings.storage_dir,
            current_principal(),
            doc_id=doc_id,
        )
        return {
            "doc_id": doc_id,
            "status": "deleted",
            "released_bytes": released.released_bytes,
        }

    if dispatcher.registry.resolve("rag.ingest") is None:
        dispatcher.registry.register("rag.ingest", ingest_handler)
    if dispatcher.registry.resolve("rag.delete") is None:
        dispatcher.registry.register("rag.delete", delete_handler)


def bind_rag_query_workflow_handler(
    dispatcher: WorkflowDispatcher,
    *,
    settings: Settings,
    main_llm: LLMPort,
    rag: RagPort,
    web: WebSearchPort,
) -> None:
    async def query_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        result = await retrieve_and_answer(
            main_llm=main_llm,
            fast_llm=main_llm,
            fast_model=settings.llm_fast_model,
            main_model=settings.llm_main_model,
            fast_timeout_s=settings.llm_fast_classification_timeout_s,
            rag=rag,
            web=web,
            question=str(payload["question"]),
            rag_top_k=int(payload["rag_top_k"]),
            web_top_n=int(payload["web_top_n"]),
            stream=False,
        )
        chunks: list[str] = []
        async for chunk in result.chunks:
            if context.cancel_event.is_set():
                raise asyncio.CancelledError
            chunks.append(chunk)
        return {
            "meta": {
                "chosen_source": result.retrieval.chosen_source,
                "rag_count": len(result.retrieval.rag_chunks),
                "web_count": len(result.retrieval.web_hits),
                "citations": [
                    {
                        "kind": "rag",
                        "doc_id": item.doc_id,
                        "chunk_id": item.chunk_id,
                        "score": item.score,
                    }
                    for item in result.retrieval.rag_chunks
                ]
                + [
                    {
                        "kind": "web",
                        "url": item.url,
                        "title": item.title,
                        "source": item.source,
                    }
                    for item in result.retrieval.web_hits
                ],
            },
            "chunks": chunks,
        }

    if dispatcher.registry.resolve("rag.query") is None:
        dispatcher.registry.register("rag.query", query_handler)


@router.post("/rag/ingest")
async def rag_ingest(  # noqa: PLR0915 - durable upload lifecycle stays linear
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    source: str = Form("upload"),
    source_path: str | None = Form(None),
    rag: RagPort = Depends(get_rag),
    settings: Settings = Depends(get_settings),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
    governor: PrincipalGovernor = Depends(get_quota_governor),
) -> dict[str, str]:
    """通用文档入库。支持的扩展名见 `parsers.SUPPORTED_EXTS`。

    上限：用户拖入使用 settings.upload_max_file_mb；工作区来源使用
    settings.workspace_max_file_mb。
    """
    from app.adapters.rag.parsers import SUPPORTED_EXTS

    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower() or ""
    if suffix not in SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported file type: {suffix or '(no extension)'}; "
                f"supported: {', '.join(sorted(SUPPORTED_EXTS))}"
            ),
        )
    principal = current_principal()
    requested_source = source if source in {"upload", "workspace"} else "upload"
    normalized_source = "upload" if principal.mode == "public" else requested_source
    limit_mb = (
        settings.workspace_max_file_mb
        if normalized_source == "workspace"
        else settings.upload_max_file_mb
    )
    normalized_source_path = (
        source_path.strip()
        if normalized_source == "workspace" and source_path and source_path.strip()
        else None
    )
    max_bytes = int(limit_mb * 1024 * 1024)
    try:
        upload = await read_limited_upload(
            file,
            max_bytes=max_bytes,
            chunk_bytes=settings.upload_read_chunk_bytes,
            governor=governor,
            principal=principal,
            persistent=False,
            upload_reservation=getattr(request.state, "upload_quota_reservation", None),
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail="document upload too large") from exc
    digest = hashlib.sha256(upload.data).hexdigest()
    active_key = f"rag.ingest:{normalized_source}:{digest}"
    bind_rag_workflow_handlers(dispatcher, rag, settings)
    active = await dispatcher.service.get_active_by_active_key(active_key)
    proposed_run_id = active.run_id if active is not None else new_workflow_run_id()
    claim = await claim_rag_content(
        settings.db_path,
        principal,
        content_hash=digest,
        size_bytes=upload.size_bytes,
        workflow_run_id=proposed_run_id,
        file_suffix=suffix,
        storage_limit=settings.quota_storage_bytes,
    )
    if claim.state == "ready" and claim.doc_id:
        return {"doc_id": claim.doc_id, "title": title or filename}

    existing_run = await dispatcher.service.get_run(claim.workflow_run_id)
    if existing_run is not None and existing_run.state in TERMINAL_WORKFLOW_STATES:
        output_doc_id = existing_run.output.get("doc_id")
        if existing_run.state == "succeeded" and output_doc_id:
            await stage_rag_content_blob(
                settings.db_path,
                settings.storage_dir,
                principal,
                content_hash=digest,
                workflow_run_id=claim.workflow_run_id,
                content=upload.data,
            )
            await bind_rag_content_doc(
                settings.db_path,
                principal,
                content_hash=digest,
                workflow_run_id=claim.workflow_run_id,
                doc_id=str(output_doc_id),
            )
            return {"doc_id": str(output_doc_id), "title": title or filename}
        await release_rag_content_claim(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
        )
        claim = await claim_rag_content(
            settings.db_path,
            principal,
            content_hash=digest,
            size_bytes=upload.size_bytes,
            workflow_run_id=new_workflow_run_id(),
            file_suffix=suffix,
            storage_limit=settings.quota_storage_bytes,
        )
        existing_run = None

    try:
        await stage_rag_content_blob(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
            workflow_run_id=claim.workflow_run_id,
            content=upload.data,
        )
    except BaseException:
        await release_rag_content_claim(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
        )
        raise

    body = (
        _resume_workflow_body(existing_run)
        if existing_run is not None
        else WorkflowRunCreate(
            kind="rag.ingest",
            source=normalized_source,
            title=title or filename,
            intent_text=f"Ingest {title or filename}",
            input={
                "title": title or filename,
                "source": normalized_source,
                "source_path": normalized_source_path,
                "content_hash": digest,
            },
            timeout_s=120,
            active_key=active_key,
        )
    )
    durable = await dispatcher.service.create_run(body, run_id=claim.workflow_run_id)
    if durable.run_id != claim.workflow_run_id:
        await release_rag_content_claim(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
        )
        claim = await claim_rag_content(
            settings.db_path,
            principal,
            content_hash=digest,
            size_bytes=upload.size_bytes,
            workflow_run_id=durable.run_id,
            file_suffix=suffix,
            storage_limit=settings.quota_storage_bytes,
        )
        await stage_rag_content_blob(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
            workflow_run_id=claim.workflow_run_id,
            content=upload.data,
        )
    try:
        dispatched = await dispatcher.dispatch(body)
        done = await dispatcher.wait_succeeded(dispatched.run_id)
    except WorkflowExecutionError as exc:
        current = await get_rag_content_claim(
            settings.db_path,
            principal,
            content_hash=digest,
        )
        if current is not None and current.state == "ready" and current.doc_id:
            return {"doc_id": current.doc_id, "title": title or filename}
        await release_rag_content_claim(
            settings.db_path,
            settings.storage_dir,
            principal,
            content_hash=digest,
        )
        raise InternalHTTPException(status_code=400, detail=str(exc)) from exc
    doc_id = str(done.output["doc_id"])
    await bind_rag_content_doc(
        settings.db_path,
        principal,
        content_hash=digest,
        workflow_run_id=done.run_id,
        doc_id=doc_id,
    )
    return {"doc_id": doc_id, "title": str(done.output["title"])}


@router.get("/rag/stats")
async def rag_stats(rag: RagPort = Depends(get_rag)) -> dict[str, object]:
    # 兼容 Protocol：BM25Rag 实例才有 stats()
    stats = getattr(rag, "stats", None)
    if stats is None:
        return {"n_chunks": -1, "n_docs": -1}
    return stats()  # type: ignore[no-any-return]


@router.get("/rag/docs")
async def rag_docs(rag: RagPort = Depends(get_rag)) -> dict[str, object]:
    """列出所有已入库文档（按 source 分组前端展示）。"""
    docs = await rag.list_docs()
    by_source: dict[str, list[dict[str, object]]] = {}
    for d in docs:
        by_source.setdefault(str(d.get("source", "unknown")), []).append(d)
    return project_client_dict(
        {"total": len(docs), "by_source": by_source, "docs": docs},
        current_principal(),
    )


@router.delete("/rag/docs/{doc_id}")
async def rag_doc_delete(
    doc_id: str,
    rag: RagPort = Depends(get_rag),
    settings: Settings = Depends(get_settings),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
) -> dict[str, str]:
    bind_rag_workflow_handlers(dispatcher, rag, settings)
    try:
        done = await dispatcher.execute(
            WorkflowRunCreate(
                kind="rag.delete",
                source="rag_api",
                intent_text=f"Delete RAG document {doc_id}",
                input={"doc_id": doc_id},
                timeout_s=30,
                active_key=f"rag.delete:{doc_id}",
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"doc_id": str(done.output["doc_id"]), "status": "deleted"}


def _sse_frame(event_type: str, payload_json: str) -> bytes:
    """Encode one explicit SSE event; JSON payload also carries its type."""

    return f"event: {event_type}\ndata: {payload_json}\n\n".encode()


def _answer_sources(retrieval: RetrievalResult) -> list[RagAnswerSource]:
    return [
        RagAnswerSource(
            kind="rag",
            doc_id=item.doc_id,
            chunk_id=item.chunk_id,
            title=item.doc_title,
            page=item.metadata.get("page"),
            score=item.score,
        )
        for item in retrieval.rag_chunks
    ] + [
        RagAnswerSource(
            kind="web",
            title=item.title,
            url=item.url,
            source=item.source,
            score=item.score,
        )
        for item in retrieval.web_hits
    ]


def _answer_trace(retrieval: RetrievalResult) -> RagAnswerTrace:
    return RagAnswerTrace(
        query=retrieval.query,
        chosen_source=retrieval.chosen_source,
        arbitration=retrieval.arbitration,
        rag_count=len(retrieval.rag_chunks),
        web_count=len(retrieval.web_hits),
    )


async def _sse(request: Request, answer: AnswerStream) -> AsyncIterator[bytes]:
    """Forward real provider deltas and terminate with one done or error event.

    Protocol:
    - ``event: delta`` / ``{"type":"delta","delta":"..."}``
    - ``event: done`` / final answer + ``sources`` + ``trace`` + legacy ``meta``
    - ``event: error`` / code, message and partial-char trace; then re-raise

    StreamingResponse cancels this iterator on disconnect. No producer task is
    spawned, so cancellation reaches the provider iterator directly; explicit
    ``aclose`` is a second guard for provider-owned HTTP streams.
    """

    parts: list[str] = []
    try:
        async for chunk in answer.chunks:
            if await request.is_disconnected():
                raise asyncio.CancelledError
            parts.append(chunk)
            event = RagAnswerDeltaEvent(delta=chunk)
            yield _sse_frame(event.type, event.model_dump_json())

        sources = _answer_sources(answer.retrieval)
        trace = _answer_trace(answer.retrieval)
        done = RagAnswerDoneEvent(
            answer="".join(parts),
            sources=sources,
            trace=trace,
            meta=RagAnswerMeta(
                chosen_source=trace.chosen_source,
                rag_count=trace.rag_count,
                web_count=trace.web_count,
                citations=sources,
            ),
        )
        yield _sse_frame(done.type, done.model_dump_json())
    except asyncio.CancelledError:
        raise
    except Exception:
        partial_chars = sum(map(len, parts))
        logger.exception("RAG answer stream failed after %d chars", partial_chars)
        error = RagAnswerErrorEvent(
            trace=RagAnswerErrorTrace(partial_chars=partial_chars),
        )
        yield _sse_frame(error.type, error.model_dump_json())
        raise
    finally:
        close = getattr(answer.chunks, "aclose", None)
        if callable(close):
            await close()


@router.post("/rag/ask")
async def rag_ask(
    request: Request,
    body: AskRequest,
    settings: Settings = Depends(get_settings),
    main_llm: LLMPort = Depends(get_llm),
    rag: RagPort = Depends(get_rag),
    web: WebSearchPort = Depends(get_web),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")

    answer = await retrieve_and_answer(
        main_llm=main_llm,
        fast_llm=main_llm,
        fast_model=settings.llm_fast_model,
        main_model=settings.llm_main_model,
        fast_timeout_s=settings.llm_fast_classification_timeout_s,
        rag=rag,
        web=web,
        question=body.question.strip(),
        rag_top_k=body.rag_top_k,
        web_top_n=body.web_top_n,
        stream=True,
    )
    return StreamingResponse(
        _sse(request, answer),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )

"""HTTP API: RAG 入库 + 检索式问答。

POST /rag/ingest        — multipart 上传任意文档入库（PDF/docx/pptx/xlsx/md/txt/csv/...）
POST /rag/ask           — 检索式问答（SSE 流式）
GET  /rag/stats         — 索引诊断
GET  /rag/docs          — 列出所有已入库文档
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters.rag import BM25Rag, RagError
from app.adapters.web_search import TavilyWebSearch
from app.api.deps import get_llm_singleton as get_llm
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.web_search import WebSearchPort
from app.use_cases.retrieve_and_answer import retrieve_and_answer

router = APIRouter(tags=["rag"])


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
    question: str
    rag_top_k: int = 5
    web_top_n: int = 5


@router.post("/rag/ingest")
async def rag_ingest(
    file: UploadFile = File(...),
    title: str | None = None,
    rag: RagPort = Depends(get_rag),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """通用文档入库。支持的扩展名见 `parsers.SUPPORTED_EXTS`。

    上限：settings.upload_max_file_mb（默认 50 MB）。
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
    max_bytes = int(settings.upload_max_file_mb * 1024 * 1024)
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"file too large: {len(content) / 1e6:.1f}MB > {settings.upload_max_file_mb}MB",
        )
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        doc_id = await rag.ingest_file(
            tmp_path,
            doc_title=title or filename,
            source="upload",
        )
    except RagError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await asyncio.to_thread(Path(tmp_path).unlink, missing_ok=True)
    return {"doc_id": doc_id, "title": title or file.filename or "doc"}


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
    return {"total": len(docs), "by_source": by_source, "docs": docs}


@router.delete("/rag/docs/{doc_id}")
async def rag_doc_delete(doc_id: str, rag: RagPort = Depends(get_rag)) -> dict[str, str]:
    await rag.delete(doc_id)
    return {"doc_id": doc_id, "status": "deleted"}


async def _sse(retrieval_json: str, chunks: AsyncIterator[str]) -> AsyncIterator[bytes]:
    # 第一帧：把检索结果作为 meta 推给前端（doc_id / web url，UI 展示引用）
    yield f"data: {retrieval_json}\n\n".encode()
    async for chunk in chunks:
        payload = json.dumps({"delta": chunk}, ensure_ascii=False)
        yield f"data: {payload}\n\n".encode()
    yield b"data: [DONE]\n\n"


@router.post("/rag/ask")
async def rag_ask(
    body: AskRequest,
    settings: Settings = Depends(get_settings),
    main_llm: LLMPort = Depends(get_llm),
    rag: RagPort = Depends(get_rag),
    web: WebSearchPort = Depends(get_web),
) -> StreamingResponse:
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="question empty")

    # fast_llm 复用 main_llm 实例（OpenAICompatibleLLM 内部按 model 路由到 fast/main 通道）
    result = await retrieve_and_answer(
        main_llm=main_llm,
        fast_llm=main_llm,
        fast_model=settings.llm_fast_model,
        rag=rag,
        web=web,
        question=body.question,
        rag_top_k=body.rag_top_k,
        web_top_n=body.web_top_n,
    )
    retrieval_meta = json.dumps(
        {
            "meta": {
                "chosen_source": result.retrieval.chosen_source,
                "rag_count": len(result.retrieval.rag_chunks),
                "web_count": len(result.retrieval.web_hits),
                "citations": [
                    {"kind": "rag", "doc_id": c.doc_id, "chunk_id": c.chunk_id, "score": c.score}
                    for c in result.retrieval.rag_chunks
                ]
                + [
                    {"kind": "web", "url": h.url, "title": h.title, "source": h.source}
                    for h in result.retrieval.web_hits
                ],
            }
        },
        ensure_ascii=False,
    )
    return StreamingResponse(_sse(retrieval_meta, result.chunks), media_type="text/event-stream")

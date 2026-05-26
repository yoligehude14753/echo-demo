"""RAG Port：jieba + BM25Okapi，多文档+会议统一索引。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.rag import RagChunk


@runtime_checkable
class RagPort(Protocol):
    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        """返回 doc_id。"""

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        """会议结束后把纪要+逐字稿入库。返回 doc_id。"""

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]: ...

    async def delete(self, doc_id: str) -> None: ...

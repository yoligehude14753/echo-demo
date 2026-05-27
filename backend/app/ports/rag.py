"""RAG Port：jieba + BM25Okapi，多文档+会议统一索引。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.rag import RagChunk


@runtime_checkable
class RagPort(Protocol):
    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        """返回 doc_id。"""

    async def ingest_file(
        self,
        file_path: str,
        doc_title: str | None = None,
        *,
        source: str = "upload",
        source_path: str | None = None,
    ) -> str:
        """通用文件入库（PDF/docx/pptx/xlsx/md/txt/csv/html/...）。

        source: "upload"（用户拖入）/ "workspace"（授权工作区扫描）/ "meeting"。
        source_path: 工作区扫描时记录原始绝对路径，便于增量去重。
        返回 doc_id。
        """

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        """会议结束后把纪要+逐字稿入库。返回 doc_id。"""

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> str:
        """ambient 主链路：按日追加 STT 文本段。返回 doc_id（ambient-YYYYMMDD）。

        speaker_id/speaker_label 走 metadata，便于 RAG 检索时定位说话人。
        """

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]: ...

    async def delete(self, doc_id: str) -> None: ...

    async def find_by_source_path(self, source_path: str) -> str | None:
        """按原始绝对路径查 doc_id；不存在返回 None。"""

    async def list_docs(self) -> list[dict[str, object]]:
        """返回 [{doc_id, title, source, source_path, n_chunks, ingested_at}, ...]。"""

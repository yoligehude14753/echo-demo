"""RAG adapter: jieba 分词 + BM25Okapi 倒排索引（多文档 + 会议统一索引）。

参考 echo/experiments/2026-05-26_pdf_rag_e2e/pdf_rag_e2e.py：
- PDF: pdfplumber 按页解析（pypdf 把 "ChatGPT" 切碎，BM25 召回归零；pdfplumber OK）
- chunk: 600 字 + 100 overlap，跨页保留页码归属
- tokenize: 中英混合，中文 jieba 词 + 英文 [a-z0-9]+ regex 兜底

索引存盘：~/.echo-demo/rag_index/{doc_id}.json
重建：进程启动时遍历目录加载所有 chunks
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings
from app.schemas.rag import RagChunk


def _tokenize_cn_en(text: str) -> list[str]:
    """jieba 中文词 + 英文/数字串。"""
    import jieba

    text = text.lower()
    tokens: list[str] = []
    for raw in jieba.cut_for_search(text):
        tok = raw.strip()
        if not tok:
            continue
        tokens.append(tok)
        tokens.extend(re.findall(r"[a-z0-9]+", tok))
    # 单字符过滤
    return [t for t in tokens if len(t) >= 1]


class RagError(RuntimeError):
    pass


class BM25Rag:
    """实现 ports.rag.RagPort。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._index_dir = Path(settings.rag_index_dir).expanduser()
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._top_k = settings.rag_top_k
        self._chunk_size = settings.rag_pdf_chunk_tokens
        self._chunk_overlap = settings.rag_pdf_chunk_overlap

        self._chunks: list[RagChunk] = []
        self._tokens: list[list[str]] = []
        self._bm25: Any | None = None
        self._lock = asyncio.Lock()
        self._load_index()

    def _load_index(self) -> None:
        """启动时把磁盘上所有 doc 的 chunks 全部加载进内存。"""
        for f in self._index_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for c in data.get("chunks", []):
                self._chunks.append(RagChunk(**c))
                self._tokens.append(c.get("tokens") or _tokenize_cn_en(c["text"]))
        self._rebuild_bm25()

    def _rebuild_bm25(self) -> None:
        if not self._tokens:
            self._bm25 = None
            return
        from rank_bm25 import BM25Okapi

        self._bm25 = BM25Okapi(self._tokens)

    def _persist_doc(self, doc_id: str, doc_title: str, chunks: list[RagChunk]) -> None:
        payload = {
            "doc_id": doc_id,
            "doc_title": doc_title,
            "chunks": [{**c.model_dump(), "tokens": _tokenize_cn_en(c.text)} for c in chunks],
        }
        f = self._index_dir / f"{doc_id}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
        out: list[str] = []
        text = re.sub(r"\s+", " ", text).strip()
        i = 0
        while i < len(text):
            sub = text[i : i + size]
            if sub.strip():
                out.append(sub)
            if i + size >= len(text):
                break
            i += size - overlap
        return out

    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._ingest_pdf_sync, file_path, doc_title)

    def _ingest_pdf_sync(self, file_path: str, doc_title: str | None) -> str:
        try:
            import pdfplumber
        except ImportError as e:
            raise RagError("pdfplumber not installed; pip install pdfplumber") from e

        path = Path(file_path).expanduser()
        if not path.exists():
            raise RagError(f"PDF not found: {file_path}")
        title = doc_title or path.stem
        doc_id = f"pdf-{uuid.uuid4().hex[:12]}"

        chunks: list[RagChunk] = []
        with pdfplumber.open(str(path)) as pdf:
            for page_idx, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                for j, sub in enumerate(
                    self._chunk_text(text, self._chunk_size, self._chunk_overlap)
                ):
                    chunks.append(
                        RagChunk(
                            doc_id=doc_id,
                            doc_title=title,
                            chunk_id=f"{doc_id}-p{page_idx:03d}-c{j:04d}",
                            text=sub,
                            metadata={"page": str(page_idx), "kind": "pdf"},
                        )
                    )

        if not chunks:
            raise RagError(f"PDF parsed but no content: {file_path}")

        self._chunks.extend(chunks)
        self._tokens.extend(_tokenize_cn_en(c.text) for c in chunks)
        self._rebuild_bm25()
        self._persist_doc(doc_id, title, chunks)
        return doc_id

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._ingest_meeting_sync, meeting_id, transcript, title)

    def _ingest_meeting_sync(self, meeting_id: str, transcript: str, title: str) -> str:
        doc_id = f"meeting-{meeting_id}"
        chunks: list[RagChunk] = []
        for j, sub in enumerate(
            self._chunk_text(transcript, self._chunk_size, self._chunk_overlap)
        ):
            chunks.append(
                RagChunk(
                    doc_id=doc_id,
                    doc_title=title,
                    chunk_id=f"{doc_id}-c{j:04d}",
                    text=sub,
                    metadata={"kind": "meeting", "meeting_id": meeting_id},
                )
            )

        if not chunks:
            raise RagError(f"meeting transcript empty: {meeting_id}")

        # 删除同 doc_id 的旧 chunks（重新入库）
        stale_idx = [i for i, c in enumerate(self._chunks) if c.doc_id == doc_id]
        for i in reversed(stale_idx):
            del self._chunks[i]
            del self._tokens[i]

        self._chunks.extend(chunks)
        self._tokens.extend(_tokenize_cn_en(c.text) for c in chunks)
        self._rebuild_bm25()
        self._persist_doc(doc_id, title, chunks)
        return doc_id

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        async with self._lock:
            if self._bm25 is None or not self._chunks:
                return []
            tokens = _tokenize_cn_en(query)
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
            # 小语料下 BM25 idf 可能给负权重，但 ranking 仍然有意义 → 不过滤分数
            ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[
                : top_k or self._top_k
            ]
            out: list[RagChunk] = []
            for idx, score in ranked:
                c = self._chunks[idx]
                out.append(
                    RagChunk(
                        doc_id=c.doc_id,
                        doc_title=c.doc_title,
                        chunk_id=c.chunk_id,
                        text=c.text,
                        score=float(score),
                        metadata=c.metadata,
                    )
                )
            return out

    async def delete(self, doc_id: str) -> None:
        async with self._lock:
            keep_chunks: list[RagChunk] = []
            keep_tokens: list[list[str]] = []
            for c, t in zip(self._chunks, self._tokens, strict=True):
                if c.doc_id != doc_id:
                    keep_chunks.append(c)
                    keep_tokens.append(t)
            self._chunks = keep_chunks
            self._tokens = keep_tokens
            self._rebuild_bm25()
            f = self._index_dir / f"{doc_id}.json"
            if f.exists():
                f.unlink()

    def stats(self) -> dict[str, Any]:
        """诊断用。"""
        doc_ids: set[str] = set()
        for c in self._chunks:
            doc_ids.add(c.doc_id)
        return {
            "n_chunks": len(self._chunks),
            "n_docs": len(doc_ids),
            "ts": time.time(),
        }

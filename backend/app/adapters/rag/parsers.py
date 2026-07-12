"""文档解析：把任意格式文件抽取成 plain text，供 RAG 入库。

策略（按 06-platforms.mdc 复用优先）：
- L2：markitdown（MS 出品的万能文档→markdown 抽取，覆盖 PDF/docx/pptx/xlsx/html/csv/epub）
- L0：文本类（md/txt/json/yaml/log 等）直接 utf-8 读取，markitdown 反而不支持
- 失败容错：单文件失败抛 ParseError，调用方决定跳过/中断
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

# binary 文档走 markitdown；text 文档直读
_MARKITDOWN_EXTS = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".csv",
    ".epub",
    ".msg",
    ".eml",
}
_TEXT_EXTS = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".log",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".srt",
    ".vtt",
    ".sql",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".zsh",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".conf",
}

SUPPORTED_EXTS: frozenset[str] = frozenset(_MARKITDOWN_EXTS | _TEXT_EXTS)

_EPUB_MAX_ENTRIES = 2_048
_EPUB_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_EPUB_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class ParseError(RuntimeError):
    pass


def is_supported(ext: str) -> bool:
    return ext.lower() in SUPPORTED_EXTS


def parse_to_text(file_path: str | Path) -> str:
    """文件 → plain text。失败抛 ParseError。"""
    p = Path(file_path).expanduser()
    if not p.exists() or not p.is_file():
        raise ParseError(f"file not found: {p}")
    ext = p.suffix.lower()

    if ext in _TEXT_EXTS:
        return _read_text(p)
    if ext == ".epub":
        return _epub_extract(p)
    if ext in _MARKITDOWN_EXTS:
        return _markitdown_extract(p)
    raise ParseError(f"unsupported extension: {ext} (file={p.name})")


def _read_text(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ParseError(f"cannot decode as text: {p.name}")


def _markitdown_extract(p: Path) -> str:
    try:
        from markitdown import MarkItDown
    except ImportError as e:
        raise ParseError("markitdown not installed; pip install markitdown") from e
    try:
        md = MarkItDown()
        result = md.convert(str(p))
    except Exception as e:
        raise ParseError(f"markitdown failed on {p.name}: {e}") from e
    text = (result.text_content or "").strip()
    if not text:
        raise ParseError(f"empty content after parse: {p.name}")
    return text


def _safe_epub_member(raw: str) -> str:
    candidate = unquote(urlsplit(raw).path).replace("\\", "/")
    normalized = posixpath.normpath(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or normalized in {".", ".."}
        or normalized.startswith("../")
    ):
        raise ParseError("unsafe EPUB archive path")
    return normalized


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epub_reading_order(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    container_name = "META-INF/container.xml"
    if container_name not in names:
        return []
    container = ElementTree.fromstring(archive.read(container_name))
    rootfile = next(
        (
            element.attrib.get("full-path", "")
            for element in container.iter()
            if _xml_local_name(element.tag) == "rootfile"
        ),
        "",
    )
    if not rootfile:
        return []
    opf_name = _safe_epub_member(rootfile)
    if opf_name not in names:
        return []
    opf = ElementTree.fromstring(archive.read(opf_name))
    opf_dir = posixpath.dirname(opf_name)
    manifest: dict[str, str] = {}
    for element in opf.iter():
        if _xml_local_name(element.tag) != "item":
            continue
        item_id = element.attrib.get("id", "")
        media_type = element.attrib.get("media-type", "").lower()
        href = element.attrib.get("href", "")
        if item_id and href and media_type in {"application/xhtml+xml", "text/html"}:
            manifest[item_id] = _safe_epub_member(posixpath.join(opf_dir, href))
    return [
        manifest[item_id]
        for element in opf.iter()
        if _xml_local_name(element.tag) == "itemref"
        and (item_id := element.attrib.get("idref", "")) in manifest
        and manifest[item_id] in names
    ]


def _epub_extract(p: Path) -> str:
    try:
        with zipfile.ZipFile(p) as archive:
            infos = archive.infolist()
            if len(infos) > _EPUB_MAX_ENTRIES:
                raise ParseError("EPUB contains too many archive entries")
            names: set[str] = set()
            total_size = 0
            for info in infos:
                name = _safe_epub_member(info.filename)
                if info.flag_bits & 0x1:
                    raise ParseError("encrypted EPUB entries are unsupported")
                if info.file_size > _EPUB_MAX_DOCUMENT_BYTES:
                    raise ParseError("EPUB entry exceeds parser limit")
                total_size += info.file_size
                if total_size > _EPUB_MAX_UNCOMPRESSED_BYTES:
                    raise ParseError("EPUB uncompressed content exceeds parser limit")
                names.add(name)

            ordered = _epub_reading_order(archive, names)
            if not ordered:
                ordered = sorted(
                    name for name in names if Path(name).suffix.lower() in {".xhtml", ".html"}
                )
            if not ordered:
                raise ParseError("EPUB contains no readable document")

            from bs4 import BeautifulSoup

            sections = [
                BeautifulSoup(archive.read(name), "html.parser").get_text("\n", strip=True)
                for name in ordered
            ]
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ParseError(f"invalid EPUB: {p.name}") from exc
    text = "\n\n".join(section for section in sections if section).strip()
    if not text:
        raise ParseError(f"empty content after parse: {p.name}")
    return text

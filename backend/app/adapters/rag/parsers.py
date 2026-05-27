"""文档解析：把任意格式文件抽取成 plain text，供 RAG 入库。

策略（按 06-platforms.mdc 复用优先）：
- L2：markitdown（MS 出品的万能文档→markdown 抽取，覆盖 PDF/docx/pptx/xlsx/html/csv/epub）
- L0：文本类（md/txt/json/yaml/log 等）直接 utf-8 读取，markitdown 反而不支持
- 失败容错：单文件失败抛 ParseError，调用方决定跳过/中断
"""

from __future__ import annotations

from pathlib import Path

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

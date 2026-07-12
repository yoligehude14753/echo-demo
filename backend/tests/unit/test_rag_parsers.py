from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from app.adapters.rag.parsers import ParseError, parse_to_text


def _write_epub(path: Path, *, extra_name: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "META-INF/container.xml",
            "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
            "<rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>",
        )
        archive.writestr(
            "OEBPS/content.opf",
            "<package xmlns='http://www.idpf.org/2007/opf'>"
            "<manifest><item id='chapter' href='chapter.xhtml' "
            "media-type='application/xhtml+xml'/></manifest>"
            "<spine><itemref idref='chapter'/></spine></package>",
        )
        archive.writestr(
            "OEBPS/chapter.xhtml",
            "<html><body><h1>EchoDesk EPUB</h1><p>owner scoped knowledge</p></body></html>",
        )
        if extra_name is not None:
            archive.writestr(extra_name, "unsafe")


@pytest.mark.unit
def test_epub_parser_follows_spine_and_extracts_text(tmp_path: Path) -> None:
    epub = tmp_path / "knowledge.epub"
    _write_epub(epub)

    text = parse_to_text(epub)

    assert "EchoDesk EPUB" in text
    assert "owner scoped knowledge" in text


@pytest.mark.unit
def test_epub_parser_rejects_archive_path_traversal(tmp_path: Path) -> None:
    epub = tmp_path / "unsafe.epub"
    _write_epub(epub, extra_name="../escaped.xhtml")

    with pytest.raises(ParseError, match="unsafe EPUB archive path"):
        parse_to_text(epub)

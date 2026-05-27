"""CLI 端到端测试：用 monkeypatch 替换 OpenAIClient 为 MockLLMClient，整套跑通。"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from minutes_kit.transcript_io import load_transcript


async def test_cli_e2e_with_mock_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """直接 await cli._run，patch LLM 客户端，跑完整 pipeline。

    不调用 cli.main() 以避免嵌套 asyncio.run。
    """
    from minutes_kit import cli as cli_module
    from tests.conftest import MockLLMClient

    transcript = (
        Path(__file__).resolve().parents[2]
        / "demo"
        / "sample_transcripts"
        / "meetly_demo.txt"
    )
    assert transcript.exists()

    out_dir = tmp_path / "run_001"

    monkeypatch.setattr(cli_module, "OpenAIClient", lambda model=None: MockLLMClient())

    args = cli_module._build_parser().parse_args(
        [
            "--transcript", str(transcript),
            "--out", str(out_dir),
            "--participants", "A,B,C",
            "--title-hint", "meetly demo",
            "--no-claude",
        ]
    )
    rc = await cli_module._run(args)

    # 退出码 = 1（部分降级，因为 mermaid 没装），但产物完整
    assert rc in (0, 1)
    assert (out_dir / "data.json").exists()
    assert (out_dir / "preview.html").exists()
    assert (out_dir / "minutes.docx").exists()

    # docx 有效
    with zipfile.ZipFile(out_dir / "minutes.docx") as zf:
        body = zf.read("word/document.xml").decode("utf-8")
        assert "周三例会" in body  # 来自 mock 的 title fixture

    # HTML 含核心内容
    html = (out_dir / "preview.html").read_text(encoding="utf-8")
    assert "<strong>Word 模板</strong>" in html  # mock 用 markdown 加粗
    assert "flowchart TD" in html
    assert 'class="mermaid"' in html

    # data.json 可往返
    import json
    data = json.loads((out_dir / "data.json").read_text(encoding="utf-8"))
    assert data["title"] == "周三例会"
    assert len(data["decisions"]) == 4
    assert len(data["todos"]) == 4


def test_cli_help_works():
    """CLI 至少 --help 不崩，作为 smoke test。"""
    result = subprocess.run(
        [sys.executable, "-m", "minutes_kit.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--transcript" in result.stdout
    assert "--out" in result.stdout


def test_cli_nonexistent_transcript_exits_2(tmp_path: Path):
    """文件不存在退出码 2。"""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "minutes_kit.cli",
            "--transcript",
            str(tmp_path / "does_not_exist.txt"),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "transcript 文件不存在" in result.stderr or "transcript 文件不存在" in result.stdout


def test_cli_empty_transcript_exits_2(tmp_path: Path):
    """空 transcript 退出码 2。"""
    empty = tmp_path / "empty.txt"
    empty.write_text("# 只有注释\n  \n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "minutes_kit.cli",
            "--transcript",
            str(empty),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2


def test_sample_transcripts_parse_clean():
    """两份 sample transcript 都能解析得出 turns。"""
    samples_dir = (
        Path(__file__).resolve().parents[2]
        / "demo"
        / "sample_transcripts"
    )
    meetly = load_transcript(samples_dir / "meetly_demo.txt")
    assert len(meetly) > 15
    assert all(t.speaker in ("A", "B", "C") for t in meetly)
    assert all(t.ts for t in meetly)

    echo = load_transcript(samples_dir / "echo_real_meeting.txt")
    assert len(echo) > 15
    # echo 是无 diarization 的 ASR 流，所有 speaker 都应该是 "?"
    assert all(t.speaker == "?" for t in echo)

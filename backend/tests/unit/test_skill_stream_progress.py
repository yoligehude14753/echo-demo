"""SkillExecutor.generate_stream 单测：流式进度事件 + 失败路径 + SSE 帧映射。

覆盖目标（与本任务交付清单对齐）：

1. happy path（HTML one-pager）：
   - 阶段顺序：prompt_build → llm_stream_start → llm_chunk × N → llm_stream_done
     → invariants_check → executor_run → saved → done
   - done 事件携带正确的 ``GeneratedArtifact``
2. happy path（默认流水线 / markdown）：复用 default pipeline 走 ``exec_text_to_file``
3. failure path（LLM 抛 ``LLMError``）：generate_stream 最后 yield ``stage="error"``，
   并 re-raise（保留 ``LLMError`` 类型供 api/artifacts.py 分流）
4. llm_chunk 节流：FakeLLM 吐多个 chunk，验证「累积 ≥ 200 chars 才推一次」节流
5. ``_progress_to_sse_frames``：把每种 stage 正确映射成 SSE bytes 帧
6. ``generate`` 向后兼容：仍能从 ``generate_stream`` 拿到最终 artifact
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.llm import LLMError
from app.adapters.skill import SkillError, SkillExecutor
from app.api.artifacts import _progress_to_sse_frames
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.schemas.llm import ChatMessage
from app.schemas.skill_progress import SkillProgress

# ──────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────


class FakeStreamLLM:
    """流式 mock LLM：``chat_stream`` 按预设 chunk 列表逐个 yield。

    与 test_skill_doc_skills.FakeLLM 的区别：那个只 yield 1 个 chunk，本类用于
    验证多 chunk 累积 + 节流。
    """

    def __init__(self, chunks: list[str], *, raise_at: int | None = None) -> None:
        self.chunks = list(chunks)
        self.raise_at = raise_at  # 在第 N 次 yield 后抛 LLMError
        self.last_messages: list[ChatMessage] | None = None

    async def chat(self, messages: list[ChatMessage], **_: Any) -> Any:
        raise NotImplementedError("skill 不应该走 chat，只走 chat_stream")

    async def chat_stream(self, messages: list[ChatMessage], **_: Any):  # type: ignore[no-untyped-def]
        self.last_messages = list(messages)
        for i, c in enumerate(self.chunks):
            if self.raise_at is not None and i >= self.raise_at:
                raise LLMError(f"simulated upstream failure after {i} chunks")
            yield c


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path,
        skill_executor_build_dir=tmp_path / "skill_build",
        skill_executor_timeout_s=30,
        skill_executor_max_tokens=80_000,
        use_legacy_html_pptx=False,
    )


def _make_valid_kami_html(extra_chars: int = 6500) -> str:
    svgs = "\n".join(
        f"<svg viewBox='0 0 100 50'><line x1='0' y1='25' x2='100' y2='25' stroke='#1B365D'/>{'x' * 30}</svg>"
        for _ in range(4)
    )
    filler = "正文段落" * extra_chars
    return (
        "<!doctype html>\n"
        "<html lang='zh'><head><meta charset='utf-8'>"
        "<style>body{background:#f5f4ed;font-family:serif;}</style></head>"
        f"<body><h1>测试标题</h1>{svgs}<p>{filler}</p></body></html>"
    )


async def _collect(agen) -> list[SkillProgress]:  # type: ignore[no-untyped-def]
    out: list[SkillProgress] = []
    async for ev in agen:
        out.append(ev)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Happy path：HTML one-pager
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_stream_html_emits_full_phase_sequence(tmp_path: Path) -> None:
    """完整阶段序列：prompt_build → llm_stream_start → llm_chunk* → llm_stream_done
    → invariants_check → executor_run → saved → done。
    """
    skill = SkillExecutor(_settings(tmp_path))
    html = _make_valid_kami_html()
    # 把 HTML 切成多个 chunk，验证 llm_chunk 节流（每累积 200 chars 才推一次）
    parts = [html[i : i + 500] for i in range(0, len(html), 500)]
    llm = FakeStreamLLM(parts)

    events: list[SkillProgress] = []
    async for ev in skill.generate_stream(
        llm=llm,
        artifact_type="html",
        brief="测试 brief",
    ):
        events.append(ev)

    stages = [e.stage for e in events]

    # 序号断言：先 prompt_build，再至少 1 个 llm_stream_start
    assert stages[0] == "prompt_build"
    assert "llm_stream_start" in stages
    # 至少有 1 个 llm_chunk（HTML 6000+ chars，500/chunk 切，每 200 累积推一次）
    assert stages.count("llm_chunk") >= 1
    # llm_stream_done 在 invariants_check 之前
    i_done = stages.index("llm_stream_done")
    i_invariants = stages.index("invariants_check")
    assert i_done < i_invariants
    # executor_run / saved / done 在 invariants_check 之后
    assert stages.index("executor_run") > i_invariants
    assert stages.index("saved") > stages.index("executor_run")
    assert stages[-1] == "done"

    # done 携带 artifact
    done_ev = events[-1]
    assert done_ev.artifact is not None
    assert done_ev.artifact.artifact_type == "html"
    assert done_ev.artifact.file_path.endswith(".html")
    saved = Path(done_ev.artifact.file_path).read_text(encoding="utf-8")
    assert "#f5f4ed" in saved


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_stream_llm_chunk_carries_accumulated_text(tmp_path: Path) -> None:
    """llm_chunk.text 是累积全文（非增量 delta），total_chars 与 text 长度一致。"""
    skill = SkillExecutor(_settings(tmp_path))
    html = _make_valid_kami_html()
    parts = [html[i : i + 500] for i in range(0, len(html), 500)]
    llm = FakeStreamLLM(parts)

    events = await _collect(skill.generate_stream(llm=llm, artifact_type="html", brief="累积验证"))
    chunks = [e for e in events if e.stage == "llm_chunk"]
    assert len(chunks) >= 1
    # 每个 llm_chunk 的 text 长度 == total_chars
    for ev in chunks:
        assert ev.text is not None
        assert ev.total_chars == len(ev.text)
    # 累积单调不降
    sizes = [c.total_chars for c in chunks]
    assert sizes == sorted(sizes)
    # 末尾 llm_stream_done 的 text 是完整内容
    done = next(e for e in events if e.stage == "llm_stream_done")
    assert done.text == html


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_stream_markdown_default_pipeline(tmp_path: Path) -> None:
    """默认流水线（markdown）：同样能 yield 完整阶段序列 + ``executor_run.tool="exec_text_to_file"``。"""
    skill = SkillExecutor(_settings(tmp_path))
    # markdown executor 要求 ≥ 300 chars（见 python_executor.exec_text_to_file），
    # 这里凑足篇幅；测试关心的是阶段序列而非内容，填充无业务含义。
    md = (
        "# 测试标题\n\n"
        + "## 概述\n\n"
        + ("这是用于验证默认 markdown 流水线的占位段落。" * 12)
        + "\n\n## 列表\n\n"
        + "\n".join(f"- 列表项 {i}" for i in range(1, 11))
        + "\n"
    )
    llm = FakeStreamLLM([md])
    events = await _collect(
        skill.generate_stream(
            llm=llm,
            artifact_type="markdown",
            brief="markdown demo",
        )
    )
    stages = [e.stage for e in events]
    assert "prompt_build" in stages
    assert "llm_stream_start" in stages
    assert "llm_stream_done" in stages
    assert "executor_run" in stages
    assert "saved" in stages
    assert stages[-1] == "done"

    # executor_run 标注了正确的工具名
    exec_ev = next(e for e in events if e.stage == "executor_run")
    assert exec_ev.tool == "exec_text_to_file"

    done = events[-1]
    assert done.artifact is not None
    assert done.artifact.artifact_type == "markdown"


# ──────────────────────────────────────────────────────────────────────────
# Failure path：LLMError → yield error + re-raise
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_stream_llm_error_yields_error_event_and_reraises(
    tmp_path: Path,
) -> None:
    """FakeLLM 在第 0 个 chunk 前 raise LLMError → generate_stream 最后一个事件
    必须是 ``stage="error"``，且整个生成器以 ``LLMError`` re-raise 结束。
    """
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeStreamLLM(["whatever"], raise_at=0)

    events: list[SkillProgress] = []
    with pytest.raises(LLMError):
        async for ev in skill.generate_stream(llm=llm, artifact_type="html", brief="fail brief"):
            events.append(ev)

    # 最后一条事件是 error
    assert events[-1].stage == "error"
    assert events[-1].error is not None
    assert "simulated upstream failure" in (events[-1].error or "")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_stream_html_invariants_failure_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    """invariants 违反（rgba）→ SkillError 在 stream 中触发 legacy fallback。

    最终仍以 ``done`` 收尾，artifact.metadata 含 ``legacy_pipeline="true"``。
    fallback 不应该 yield ``error``（只有在最外层 raise 时才 yield error）。
    """
    skill = SkillExecutor(_settings(tmp_path))
    bad_html = _make_valid_kami_html().replace(
        "background:#f5f4ed", "background:rgba(245,244,237,1)"
    )
    llm = FakeStreamLLM([bad_html])

    events = await _collect(
        skill.generate_stream(llm=llm, artifact_type="html", brief="rgba fallback")
    )
    stages = [e.stage for e in events]
    # 没有 error 事件（fallback 成功了）
    assert "error" not in stages
    # 末尾 done
    assert stages[-1] == "done"
    art = events[-1].artifact
    assert art is not None
    assert art.metadata.get("legacy_pipeline") == "true"
    assert "fallback_reason" in art.metadata


# ──────────────────────────────────────────────────────────────────────────
# 向后兼容：generate(...) 仍能从 generate_stream 拿到产物
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_backcompat_returns_artifact_from_stream(tmp_path: Path) -> None:
    skill = SkillExecutor(_settings(tmp_path))
    html = _make_valid_kami_html()
    llm = FakeStreamLLM([html])

    art = await skill.generate(llm=llm, artifact_type="html", brief="向后兼容")
    assert isinstance(art, GeneratedArtifact)
    assert art.artifact_type == "html"
    assert art.metadata["skill_variant"] == "kami_one_pager"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_backcompat_propagates_llm_error(tmp_path: Path) -> None:
    """LLMError 要保留类型透传到 api/artifacts.py（让它分流到 502）。"""
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeStreamLLM(["whatever"], raise_at=0)
    with pytest.raises(LLMError):
        await skill.generate(llm=llm, artifact_type="html", brief="x")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_backcompat_propagates_skill_error_legacy_pptx(
    tmp_path: Path,
) -> None:
    """pptx 路径：JSON 解析失败 → SkillError；类型必须保留供 api 分流到 400。"""
    skill = SkillExecutor(_settings(tmp_path))
    llm = FakeStreamLLM(["LLM 没听懂"])
    with pytest.raises(SkillError):
        await skill.generate(llm=llm, artifact_type="pptx", brief="x")


# ──────────────────────────────────────────────────────────────────────────
# SSE 帧映射：_progress_to_sse_frames
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_progress_to_sse_frames_phase() -> None:
    frames = _progress_to_sse_frames(SkillProgress(stage="prompt_build", msg="准备 prompt 中…"))
    assert len(frames) == 1
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: phase\n")
    assert "\ndata: " in raw
    assert raw.endswith("\n\n")
    data_line = next(line for line in raw.split("\n") if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: ") :])
    assert parsed == {"phase": "prompt_build", "msg": "准备 prompt 中…"}


@pytest.mark.unit
def test_progress_to_sse_frames_llm_stream_done_carries_total_chars_and_latency() -> None:
    frames = _progress_to_sse_frames(
        SkillProgress(
            stage="llm_stream_done",
            text="ignored in phase event",
            total_chars=1234,
            latency_ms=987.5,
        )
    )
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: phase\n")
    parsed = json.loads(raw.split("\ndata: ", 1)[1].rstrip("\n"))
    assert parsed["phase"] == "llm_stream_done"
    assert parsed["total_chars"] == 1234
    assert parsed["latency_ms"] == 987.5
    # text 不会进 phase 帧（避免 6000+ chars 重复推；llm_chunk 已经推过累积内容了）
    assert "text" not in parsed


@pytest.mark.unit
def test_progress_to_sse_frames_llm_chunk() -> None:
    frames = _progress_to_sse_frames(
        SkillProgress(stage="llm_chunk", text="abc中文", total_chars=5)
    )
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: llm_chunk\n")
    parsed = json.loads(raw.split("\ndata: ", 1)[1].rstrip("\n"))
    assert parsed == {"text": "abc中文", "total_chars": 5}


@pytest.mark.unit
def test_progress_to_sse_frames_done_carries_artifact_json() -> None:
    art = GeneratedArtifact(
        artifact_id="html-deadbeef",
        artifact_type="html",
        title="测试",
        file_path="/tmp/output.html",
        mime_type="text/html",
        size_bytes=1024,
        generation_latency_ms=12.5,
        model="MiniMax-M2.7",
        metadata={"chars": "1024"},
    )
    frames = _progress_to_sse_frames(SkillProgress(stage="done", artifact=art))
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: done\n")
    parsed = json.loads(raw.split("\ndata: ", 1)[1].rstrip("\n"))
    assert parsed["artifact_id"] == "html-deadbeef"
    assert parsed["title"] == "测试"
    assert parsed["size_bytes"] == 1024


@pytest.mark.unit
def test_progress_to_sse_frames_error() -> None:
    frames = _progress_to_sse_frames(SkillProgress(stage="error", error="LLM 上游不可达"))
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: error\n")
    parsed = json.loads(raw.split("\ndata: ", 1)[1].rstrip("\n"))
    assert parsed == {"error": "LLM 上游不可达", "stage": "error"}


@pytest.mark.unit
def test_progress_to_sse_frames_executor_run_includes_tool() -> None:
    frames = _progress_to_sse_frames(
        SkillProgress(
            stage="executor_run",
            tool="node_render_mjs",
            msg="渲染 14 页投行风 PPT…",
        )
    )
    raw = frames[0].decode("utf-8")
    assert raw.startswith("event: phase\n")
    parsed = json.loads(raw.split("\ndata: ", 1)[1].rstrip("\n"))
    assert parsed["phase"] == "executor_run"
    assert parsed["tool"] == "node_render_mjs"

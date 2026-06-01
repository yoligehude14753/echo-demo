"""Agent loop 单测:工具串联 / 解析容错 / 错误降级。

覆盖:
1. happy path: rag_search → web_search → generate_artifact → final
2. 简单 chat 路径: 第一步就 final
3. 工具失败: rag 抛错也能继续 web_search
4. 解析容错: 第一步 LLM 输出无效 JSON, 第二步合法 JSON
5. loop_limit 强制收尾
6. LLM 调用挂掉直接 error+done
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from app.adapters.llm import LLMError
from app.adapters.skill import SkillError
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.schemas.llm import ChatMessage, LLMResponse, LLMUsage
from app.schemas.rag import RagChunk, WebHit
from app.use_cases.agent_loop import run_agent

# ──────────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────────


class _ScriptedLLM:
    """按 ``responses`` 列表顺序返回 JSON 字符串。"""

    def __init__(self, responses: Sequence[str], *, raise_after: int | None = None) -> None:
        self._responses = list(responses)
        self._raise_after = raise_after
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
        self.calls.append(list(messages))
        idx = len(self.calls) - 1
        if self._raise_after is not None and idx >= self._raise_after:
            raise LLMError("simulated llm failure")
        if idx >= len(self._responses):
            raise RuntimeError(
                f"unexpected llm call #{idx + 1}; scripted only {len(self._responses)}"
            )
        return LLMResponse(
            content=self._responses[idx],
            model="test-model",
            finish_reason="stop",
            usage=LLMUsage(),
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: list[ChatMessage], **_: Any) -> AsyncIterator[str]:
        raise NotImplementedError("agent loop should only use .chat()")
        yield ""  # pragma: no cover


class _FakeRag:
    def __init__(
        self,
        chunks: Sequence[RagChunk] | None = None,
        *,
        raise_on_query: bool = False,
    ) -> None:
        self._chunks = list(chunks or [])
        self._raise = raise_on_query
        self.queries: list[tuple[str, int]] = []

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        self.queries.append((query, top_k))
        if self._raise:
            raise RuntimeError("rag exploded")
        return self._chunks

    async def ingest_pdf(self, file_path: str, doc_title: str | None = None) -> str:
        raise NotImplementedError

    async def ingest_file(
        self,
        file_path: str,
        doc_title: str | None = None,
        *,
        source: str = "upload",
        source_path: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def ingest_meeting(self, meeting_id: str, transcript: str, title: str) -> str:
        raise NotImplementedError

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def delete(self, doc_id: str) -> None:
        raise NotImplementedError

    async def find_by_source_path(self, source_path: str) -> str | None:
        return None

    async def list_docs(self) -> list[dict[str, object]]:
        return []


class _FakeWeb:
    def __init__(self, hits: Sequence[WebHit] | None = None) -> None:
        self._hits = list(hits or [])
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, *, top_n: int = 5) -> list[WebHit]:
        self.queries.append((query, top_n))
        return self._hits


class _FakeSkill:
    def __init__(
        self,
        *,
        artifact: GeneratedArtifact | None = None,
        raise_skill: bool = False,
    ) -> None:
        self._artifact = artifact
        self._raise = raise_skill
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        llm: Any,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact:
        self.calls.append(
            {
                "artifact_type": artifact_type,
                "brief": brief,
                "extra_instructions": extra_instructions,
            }
        )
        if self._raise:
            raise SkillError("skill failed")
        assert self._artifact is not None
        return self._artifact

    def generate_stream(self, **_: Any) -> Any:  # pragma: no cover - agent doesn't use it
        raise NotImplementedError


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path,
        skill_executor_build_dir=tmp_path / "skill_build",
        skill_executor_timeout_s=30,
        skill_executor_max_tokens=12_000,
    )


def _chunk(doc_id: str, *, title: str, text: str, page: int | None = None) -> RagChunk:
    metadata: dict[str, str] = {"kind": "pdf"}
    if page is not None:
        metadata["page"] = str(page)
    return RagChunk(
        doc_id=doc_id,
        doc_title=title,
        chunk_id=f"{doc_id}-c0000",
        text=text,
        score=5.0,
        metadata=metadata,
    )


def _artifact(tmp_path: Path, artifact_type: str = "html") -> GeneratedArtifact:
    ext = {"pptx": "pptx", "xlsx": "xlsx", "word": "docx"}.get(artifact_type, artifact_type)
    return GeneratedArtifact(
        artifact_id=f"{artifact_type}-abc",
        artifact_type=artifact_type,
        title="heyi 竞品调研",
        file_path=str(tmp_path / f"{artifact_type}-abc" / f"output.{ext}"),
        mime_type="application/octet-stream",
        size_bytes=12_345,
        generation_latency_ms=4321.0,
        model="test",
        metadata={"chars": "12345"},
    )


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_happy_path_rag_then_web_then_artifact_then_final(tmp_path: Path) -> None:
    llm = _ScriptedLLM(
        [
            '{"action":"tool_call","tool":"rag_search","args":{"query":"褐蚁 HY100"},"reason":"先查手册"}',
            '{"action":"tool_call","tool":"web_search","args":{"query":"DGX Spark Mac Studio H20","top_n":3},"reason":"补充竞品行情"}',
            '{"action":"tool_call","tool":"generate_artifact","args":{"artifact_type":"html","brief":"heyi 竞品调研...","extra_instructions":"只用 brief 中事实"},"reason":"生成 HTML"}',
            '{"action":"final","answer":"已生成 html 产物 heyi 竞品调研, 见弹窗。"}',
        ]
    )
    rag = _FakeRag([_chunk("pdf-1", title="褐蚁产品手册", text="HY100 配置 ...", page=13)])
    web = _FakeWeb(
        [WebHit(title="DGX Spark", url="https://x", snippet="800GB/s", score=0.9, source="tavily")]
    )
    art = _artifact(tmp_path)
    skill = _FakeSkill(artifact=art)

    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=rag,
            web=web,
            skill=skill,
            settings=_settings(tmp_path),
            question="@echo 帮我做 heyi 竞品调研并输出 HTML",
        )
    ]

    types = [ev.type for ev in events]
    # 2 个确定性 grounding prelude + 4 个 agent plan + 3 个模型 tool_call
    assert types.count("plan") == 4
    assert types.count("tool_call") == 5
    assert types.count("tool_result") == 5
    assert types.count("artifact") == 1
    assert types[-1] == "done"
    assert types[-2] == "final"

    tool_names = [ev.payload["name"] for ev in events if ev.type == "tool_call"]
    assert tool_names == [
        "rag_search",
        "web_search",
        "rag_search",
        "web_search",
        "generate_artifact",
    ]

    final = next(ev for ev in events if ev.type == "final")
    assert "html" in final.payload["answer"]
    assert final.payload["artifact_ids"] == ["html-abc"]

    artifact_ev = next(ev for ev in events if ev.type == "artifact")
    assert artifact_ev.payload["artifact_id"] == "html-abc"
    assert artifact_ev.payload["artifact_type"] == "html"

    assert rag.queries == [
        (
            "褐蚁 HY100 HY90 heyi heyi100 产品手册 型号 配置 竞品 生态位 "
            "@echo 帮我做 heyi 竞品调研并输出 HTML",
            40,
        ),
        ("褐蚁 HY100", 20),
    ]
    assert web.queries == [
        (
            "褐蚁 HY100 本地大模型 算力一体机 竞品 市场 @echo 帮我做 heyi 竞品调研并输出 HTML",
            5,
        ),
        ("DGX Spark Mac Studio H20", 3),
    ]
    assert skill.calls[0]["artifact_type"] == "html"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_simple_chat_short_circuits_to_final(tmp_path: Path) -> None:
    """没有工具诉求时 LLM 应一步 final。"""
    llm = _ScriptedLLM(['{"action":"final","answer":"你好, 有什么我可以帮你的?"}'])
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="你好",
        )
    ]
    types = [ev.type for ev in events]
    assert types == ["plan"] + ["delta"] * (sum(1 for ev in events if ev.type == "delta")) + [
        "final",
        "done",
    ]
    final = next(ev for ev in events if ev.type == "final")
    assert "你好" in final.payload["answer"]
    assert final.payload["artifact_ids"] == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tool_failure_is_fed_back_not_fatal(tmp_path: Path) -> None:
    """RAG 抛错时 agent 应能 fallback 到 web_search 或 final, 不挂。"""
    llm = _ScriptedLLM(
        [
            '{"action":"tool_call","tool":"rag_search","args":{"query":"x"},"reason":"a"}',
            '{"action":"tool_call","tool":"web_search","args":{"query":"x"},"reason":"b"}',
            '{"action":"final","answer":"已尽力, 本地知识库没结果, web 有 1 条。"}',
        ]
    )
    rag = _FakeRag(raise_on_query=True)
    web = _FakeWeb([WebHit(title="x", url="https://y", snippet="z", score=0.5, source="ddg")])
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=rag,
            web=web,
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="@echo 查 x",
        )
    ]
    rag_result = next(
        ev for ev in events if ev.type == "tool_result" and ev.payload["name"] == "rag_search"
    )
    assert rag_result.payload["ok"] is False
    assert events[-1].type == "done"
    final = next(ev for ev in events if ev.type == "final")
    assert "web" in final.payload["answer"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_invalid_json_then_valid_recovers(tmp_path: Path) -> None:
    """LLM 第一步输出垃圾文本, 系统应提示重发, 第二步合法则继续。"""
    llm = _ScriptedLLM(
        [
            "我先想一下...",  # 无效 JSON
            '{"action":"final","answer":"想好了, 这是答案。"}',
        ]
    )
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="x",
        )
    ]
    # 第一步 invalid 不应产 tool_call 或 final
    types = [ev.type for ev in events]
    assert types.count("plan") == 2
    assert "tool_call" not in types
    assert any(ev.type == "final" for ev in events)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_invalid_json_too_many_times_falls_back_to_direct_answer(
    tmp_path: Path,
) -> None:
    """编排协议反复失败（非产物请求）→ 不再硬报错，退回纯 LLM 直答。

    脚本：前 3 次都是非法 JSON（触发 format-retry 超限），第 4 次（被
    _direct_chat_answer 复用）返回一段普通文本 → 作为 final 回答交付。
    """
    llm = _ScriptedLLM(["foo", "bar", "baz", "这是直接回答的内容。"])
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="总结一下情况",
        )
    ]
    assert not any(
        ev.type == "error" and ev.payload.get("stage") == "parse" for ev in events
    )
    final = next(ev for ev in events if ev.type == "final")
    assert "直接回答" in final.payload["answer"]
    assert events[-1].type == "done"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_parser_accepts_first_json_when_model_outputs_extra_object(tmp_path: Path) -> None:
    llm = _ScriptedLLM(
        [
            '{"action":"final","answer":"第一段"}\n{"action":"final","answer":"多余第二段"}',
        ]
    )
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="你好",
        )
    ]
    assert not any(ev.type == "error" for ev in events)
    final = next(ev for ev in events if ev.type == "final")
    assert final.payload["answer"] == "第一段"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    ("question", "artifact_type"),
    [
        ("@echo 研究大模型一体机在教育场景的应用和招投标情况，然后生成 PPT", "pptx"),
        ("@echo 调研竞品并生成 Excel 表格", "xlsx"),
        ("@echo 调研竞品并生成 Word 报告", "word"),
    ],
)
async def test_parse_failure_falls_back_to_requested_office_artifact(
    tmp_path: Path,
    question: str,
    artifact_type: str,
) -> None:
    llm = _ScriptedLLM(["坏格式"] * 4)
    skill = _FakeSkill(artifact=_artifact(tmp_path, artifact_type))
    web = _FakeWeb(
        [WebHit(title="教育一体机采购", url="https://example.com/tender", snippet="招投标信息", score=0.9, source="ddg")]
    )

    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=web,
            skill=skill,
            settings=_settings(tmp_path),
            question=question,
        )
    ]

    assert not any(ev.type == "error" for ev in events)
    assert any(ev.type == "artifact" for ev in events)
    assert skill.calls[-1]["artifact_type"] == artifact_type
    assert "用户目标" in skill.calls[-1]["brief"]
    assert "资料不足" in skill.calls[-1]["extra_instructions"]
    final = next(ev for ev in events if ev.type == "final")
    assert final.payload["artifact_ids"] == [f"{artifact_type}-abc"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_llm_failure_emits_error_then_done(tmp_path: Path) -> None:
    llm = _ScriptedLLM([], raise_after=0)
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="x",
        )
    ]
    err = next(ev for ev in events if ev.type == "error")
    assert err.payload["stage"] == "llm"
    assert events[-1].type == "done"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unknown_tool_is_reported_but_loop_continues(tmp_path: Path) -> None:
    llm = _ScriptedLLM(
        [
            '{"action":"tool_call","tool":"do_magic","args":{},"reason":"试试看"}',
            '{"action":"final","answer":"哦, 那个工具没有。"}',
        ]
    )
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="x",
        )
    ]
    tool_result = next(ev for ev in events if ev.type == "tool_result")
    assert tool_result.payload["ok"] is False
    assert "未知工具" in tool_result.payload["summary"]
    assert events[-1].type == "done"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_loop_limit_forces_final_answer_after_max_iterations(tmp_path: Path) -> None:
    """步数用尽时不再只抛错, 而是强制模型基于已有信息给一个最终回答。"""
    responses = [
        '{"action":"tool_call","tool":"rag_search","args":{"query":"q1"},"reason":"再来"}',
        '{"action":"tool_call","tool":"rag_search","args":{"query":"q2"},"reason":"再来"}',
        '{"action":"tool_call","tool":"rag_search","args":{"query":"q3"},"reason":"再来"}',
        '{"action":"final","answer":"基于已检索信息给出的最终回答。"}',  # 强制收尾这一次调用
    ]
    llm = _ScriptedLLM(responses)
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag([_chunk("d", title="t", text="x")]),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="x",
            max_iterations=3,
        )
    ]
    assert not any(
        ev.type == "error" and ev.payload.get("stage") == "loop_limit" for ev in events
    )
    final = next(ev for ev in events if ev.type == "final")
    assert "最终回答" in final.payload["answer"]
    assert events[-1].type == "done"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_repeated_identical_tool_call_is_intercepted(tmp_path: Path) -> None:
    """同一工具+完全相同参数第二次调用应被拦截, 不重复真正执行。"""
    responses = [
        '{"action":"tool_call","tool":"rag_search","args":{"query":"same"},"reason":"1"}',
        '{"action":"tool_call","tool":"rag_search","args":{"query":"same"},"reason":"2"}',
        '{"action":"final","answer":"已基于检索结果作答。"}',
    ]
    llm = _ScriptedLLM(responses)
    rag = _FakeRag([_chunk("d", title="t", text="x")])
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=rag,
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="x",
            max_iterations=5,
        )
    ]
    assert len(rag.queries) == 1  # 第二次相同调用被拦截, rag 只真正查了一次
    final = next(ev for ev in events if ev.type == "final")
    assert final.payload["answer"] == "已基于检索结果作答。"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_llm_step_retries_once_on_transient_error(tmp_path: Path) -> None:
    """单步 LLM 瞬时失败应自动重试一次, 不直接崩掉整轮对话。"""

    class _FlakyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: list[ChatMessage], **_: Any) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                raise LLMError("transient blip")
            return LLMResponse(
                content='{"action":"final","answer":"重试后成功作答。"}',
                model="test-model",
                finish_reason="stop",
                usage=LLMUsage(),
                latency_ms=1.0,
            )

        async def chat_stream(self, messages: list[ChatMessage], **_: Any) -> AsyncIterator[str]:
            raise NotImplementedError
            yield ""  # pragma: no cover

    llm = _FlakyLLM()
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="hi",
            max_iterations=3,
        )
    ]
    assert llm.calls == 2  # 第一次失败 + 重试一次成功
    final = next(ev for ev in events if ev.type == "final")
    assert "重试后成功" in final.payload["answer"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_artifact_failure_feeds_back_to_llm(tmp_path: Path) -> None:
    """skill 抛错时 agent 应把错误喂回 LLM, 让它能 final_answer 收尾。"""
    llm = _ScriptedLLM(
        [
            '{"action":"tool_call","tool":"generate_artifact","args":{"artifact_type":"html","brief":"x"},"reason":"生成"}',
            '{"action":"final","answer":"很遗憾, 生成产物失败了, 请稍后重试。"}',
        ]
    )
    skill = _FakeSkill(raise_skill=True)
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=skill,
            settings=_settings(tmp_path),
            question="@生成 HTML x",
        )
    ]
    tool_result = next(ev for ev in events if ev.type == "tool_result")
    assert tool_result.payload["ok"] is False
    assert not any(ev.type == "artifact" for ev in events)
    assert events[-1].type == "done"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_blank_question_short_circuits_with_input_error(tmp_path: Path) -> None:
    llm = _ScriptedLLM([])
    events = [
        ev
        async for ev in run_agent(
            main_llm=llm,
            rag=_FakeRag(),
            web=_FakeWeb(),
            skill=_FakeSkill(),
            settings=_settings(tmp_path),
            question="   ",
        )
    ]
    assert events[0].type == "error"
    assert events[0].payload["stage"] == "input"
    assert events[1].type == "done"
    assert llm.calls == []

"""SkillExecutor: 用 LLM 生成代码 → 执行 → 返回 GeneratedArtifact。

支持 4 种产物：word / xlsx / html /（ppt - 留接口未启用）

参考 echo experiments/2026-05-26_anthropic_skill_quality/skill_bench_v2.py。
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from app.adapters.skill.prompts import SKILL_PROMPTS
from app.adapters.skill.python_executor import exec_html_to_file, exec_python_to_artifact
from app.config import Settings
from app.ports.llm import LLMPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.llm import ChatMessage


class SkillError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    """剥掉 ```python / ```html 这种 LLM 偶尔加上的围栏。"""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


class SkillExecutor:
    """实现 ports.skill.SkillPort（4 产物生成）。"""

    SUPPORTED: frozenset[str] = frozenset({"word", "xlsx", "excel", "html"})

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._build_root = Path(settings.skill_executor_build_dir).expanduser()
        self._timeout_s = float(settings.skill_executor_timeout_s)
        self._max_tokens = settings.skill_executor_max_tokens

    async def generate(
        self,
        *,
        llm: LLMPort,
        artifact_type: str,
        brief: str,
        extra_instructions: str | None = None,
    ) -> GeneratedArtifact:
        kind = artifact_type.lower()
        if kind not in self.SUPPORTED:
            raise SkillError(
                f"unsupported artifact_type: {artifact_type} (supported: {sorted(self.SUPPORTED)})"
            )

        sys_prompt = SKILL_PROMPTS[kind]
        user_msg = brief
        if extra_instructions:
            user_msg += "\n\n额外指令：\n" + extra_instructions

        t_start = time.monotonic()
        resp = await llm.chat(
            [
                ChatMessage(role="system", content=sys_prompt),
                ChatMessage(role="user", content=user_msg),
            ],
            max_tokens=self._max_tokens,
            temperature=0.4,
            timeout_s=self._timeout_s,
        )
        code = _strip_code_fence(resp.content)
        gen_latency_ms = (time.monotonic() - t_start) * 1000.0

        artifact_id = f"{kind}-{uuid.uuid4().hex[:10]}"
        build_dir = self._build_root / artifact_id
        build_dir.mkdir(parents=True, exist_ok=True)

        if kind == "html":
            result = await exec_html_to_file(code, build_dir)
            ext = "html"
        else:
            ext_map = {"word": "docx", "xlsx": "xlsx", "excel": "xlsx"}
            ext = ext_map[kind]
            result = await exec_python_to_artifact(
                code, build_dir, expected_ext=ext, timeout_s=self._timeout_s
            )

        if not result.success or result.output_path is None:
            raise SkillError(f"skill {kind} execution failed: {result.stderr[:400]}")

        metadata: dict[str, str] = {
            "kind": kind,
            "model": resp.model,
            "exec_elapsed_s": f"{result.elapsed_s:.2f}",
            "code_size": str(len(code)),
        }

        # 简单的质量信号（KPI 数量）
        bag = code.lower()
        if kind == "html":
            metadata["chars"] = str(len(code))
            metadata["has_tailwind"] = str("tailwindcss" in bag)
            metadata["has_svg"] = str("<svg" in bag)
        elif kind in {"xlsx", "excel"}:
            metadata["formula_count"] = str(len(re.findall(r"=[A-Z]+\(", code)))

        return GeneratedArtifact(
            artifact_id=artifact_id,
            artifact_type=kind,
            file_path=str(result.output_path),
            mime_type=_mime_for(ext),
            size_bytes=result.output_path.stat().st_size,
            generation_latency_ms=gen_latency_ms + result.elapsed_s * 1000.0,
            model=resp.model,
            metadata=metadata,
        )


def _mime_for(ext: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "html": "text/html",
    }.get(ext, "application/octet-stream")

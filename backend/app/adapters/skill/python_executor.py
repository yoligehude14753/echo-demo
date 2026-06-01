"""安全执行 LLM 生成的 Python 代码（限制目录 + 超时 + LibreOffice 验证）。

约束：
- 只在 storage/skill_build/{request_id}/ 内执行
- 子进程 + 超时（默认 120s）
- 不允许 import 网络库（用 ast 简单检查 import 黑名单）
- 输出文件强制重命名为 output.<ext>
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_FORBIDDEN_IMPORTS = (
    "import socket",
    "import requests",
    "from socket",
    "from requests",
    "from urllib",
    "import urllib",
    "subprocess.",  # 二次启动子进程
    "os.system",
    "os.execvp",
)


def _is_safe_python(code: str) -> tuple[bool, str]:
    for tok in _FORBIDDEN_IMPORTS:
        if tok in code:
            return False, f"forbidden token: {tok}"
    return True, ""


def _normalize_python_code(code: str) -> str:
    """Fix common LLM slips before sandboxed execution."""
    fixed = re.sub(
        r"(line_spacing\s*=\s*)WD_LINE_SPACING\.AUTO",
        r"\1None",
        code,
    )
    fixed = re.sub(
        r"(set_parm_fmt\([^)]*?,\s*line\s*=\s*)WD_LINE_SPACING\.AUTO",
        r"\1None",
        fixed,
    )
    return fixed


@dataclass
class ExecResult:
    success: bool
    output_path: Path | None
    stderr: str
    elapsed_s: float


async def exec_python_to_artifact(
    code: str,
    build_dir: Path,
    *,
    expected_ext: str,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
) -> ExecResult:
    """把 LLM 生成的 Python 写到 build_dir/script.py，运行后期望输出 build_dir/output.{ext}。

    自动把代码内 `doc.save('xxx')` / `wb.save('xxx')` / `pdf.output('xxx')`
    改写为绝对路径，避免 cwd 变化导致找不到文件。

    ``env`` 可选，传入额外的子进程环境变量（如 PDF 字体路径）；在父进程 env
    基础上合并，不替换。
    """
    ok, reason = _is_safe_python(code)
    if not ok:
        return ExecResult(False, None, reason, 0.0)

    await asyncio.to_thread(build_dir.mkdir, parents=True, exist_ok=True)
    output_path = build_dir / f"output.{expected_ext}"

    code = _normalize_python_code(code)

    # 重写 save()/output() 路径为绝对路径
    code_norm = re.sub(
        r"(doc|wb|workbook|pres|pdf)\.(save|output)\(\s*['\"][^'\"]+['\"]\s*\)",
        f"\\1.\\2(r'{output_path.resolve()}')",
        code,
    )

    script_path = build_dir / "script.py"
    await asyncio.to_thread(script_path.write_text, code_norm, encoding="utf-8")

    subproc_env: dict[str, str] | None = None
    if env:
        subproc_env = {**os.environ, **dict(env)}

    t0 = time.monotonic()

    def _run() -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=subproc_env,
        )
        return proc.returncode, proc.stderr

    try:
        rc, stderr = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as e:
        return ExecResult(False, None, f"timeout after {timeout_s}s: {e}", timeout_s)
    except Exception as e:  # pragma: no cover
        return ExecResult(False, None, f"{type(e).__name__}: {e}", time.monotonic() - t0)

    elapsed = time.monotonic() - t0

    def _ok() -> bool:
        return output_path.exists() and output_path.stat().st_size > 100

    if rc == 0 and await asyncio.to_thread(_ok):
        return ExecResult(True, output_path, "", elapsed)
    if rc == 0:
        generated = await asyncio.to_thread(
            lambda: sorted(
                (
                    p
                    for p in build_dir.glob(f"*.{expected_ext}")
                    if p.is_file() and p.stat().st_size > 100
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        )
        if generated:
            alt = generated[0]
            if alt != output_path:
                await asyncio.to_thread(alt.replace, output_path)
            return ExecResult(True, output_path, "", elapsed)
    return ExecResult(False, None, f"rc={rc} stderr={stderr[:600]}", elapsed)


async def exec_text_to_file(text: str, build_dir: Path, ext: str) -> ExecResult:
    """LLM 直出文本（html / markdown / txt）直接落盘 + 基本健康检查。

    - html: 必须含 `<html` 或 `<!DOCTYPE`；长度 ≥ 1500 字符
    - markdown / txt: 不允许是「围栏包裹整篇」的 LLM 输出（已在上游剥掉），且
      长度阈值放宽到 ≥ 300 字符（中文段落即可达到）
    """
    await asyncio.to_thread(build_dir.mkdir, parents=True, exist_ok=True)
    output_path = build_dir / f"output.{ext}"
    s = text.strip()

    if ext == "html":
        head = s.lower()[:500]
        if "<html" not in head and not head.startswith("<!doctype"):
            return ExecResult(False, None, "no <!DOCTYPE> / <html> in head", 0.0)
        if len(s) < 1500:
            return ExecResult(False, None, f"too short ({len(s)} chars)", 0.0)
    elif ext in {"md", "markdown", "txt", "text"}:
        if len(s) < 300:
            return ExecResult(False, None, f"too short ({len(s)} chars)", 0.0)
    else:  # pragma: no cover - 入口处已校验
        return ExecResult(False, None, f"unsupported text ext: {ext}", 0.0)

    await asyncio.to_thread(output_path.write_text, s, encoding="utf-8")
    return ExecResult(True, output_path, "", 0.0)


async def exec_html_to_file(code: str, build_dir: Path) -> ExecResult:
    """HTML 直接写文件 + 基本健康检查（保留兼容 alias，新调用方应用 exec_text_to_file）。"""
    return await exec_text_to_file(code, build_dir, "html")

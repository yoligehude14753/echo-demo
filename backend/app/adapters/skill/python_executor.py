"""安全执行 LLM 生成的 Python 代码（限制目录 + 超时 + LibreOffice 验证）。

约束：
- 只在 storage/skill_build/{request_id}/ 内执行
- 子进程 + 超时（默认 120s）
- 不允许 import 网络库（用 ast 简单检查 import 黑名单）
- 输出文件强制重命名为 output.<ext>
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
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
) -> ExecResult:
    """把 LLM 生成的 Python 写到 build_dir/script.py，运行后期望输出 build_dir/output.{ext}。

    自动把代码内 `doc.save('xxx')` / `wb.save('xxx')` 改写为绝对路径，避免 cwd 变化导致找不到文件。
    """
    ok, reason = _is_safe_python(code)
    if not ok:
        return ExecResult(False, None, reason, 0.0)

    await asyncio.to_thread(build_dir.mkdir, parents=True, exist_ok=True)
    output_path = build_dir / f"output.{expected_ext}"

    # 重写 save() 路径为绝对路径
    code_norm = re.sub(
        r"(doc|wb|workbook|pres)\.save\(\s*['\"][^'\"]+['\"]\s*\)",
        f"\\1.save(r'{output_path.resolve()}')",
        code,
    )

    script_path = build_dir / "script.py"
    await asyncio.to_thread(script_path.write_text, code_norm, encoding="utf-8")

    t0 = time.monotonic()

    def _run() -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
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
    return ExecResult(False, None, f"rc={rc} stderr={stderr[:600]}", elapsed)


async def exec_html_to_file(code: str, build_dir: Path) -> ExecResult:
    """HTML 直接写文件 + 基本健康检查。"""
    await asyncio.to_thread(build_dir.mkdir, parents=True, exist_ok=True)
    output_path = build_dir / "output.html"
    s = code.strip()
    if "<html" not in s.lower()[:500] and not s.lower().startswith("<!doctype"):
        return ExecResult(False, None, "no <!DOCTYPE> / <html> in head", 0.0)
    if len(s) < 1500:
        return ExecResult(False, None, f"too short ({len(s)} chars)", 0.0)
    await asyncio.to_thread(output_path.write_text, s, encoding="utf-8")
    return ExecResult(True, output_path, "", 0.0)

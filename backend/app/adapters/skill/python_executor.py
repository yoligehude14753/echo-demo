"""安全执行 LLM 生成的 Python 代码（限制目录 + 超时 + LibreOffice 验证）。

约束：
- 只在 storage/skill_build/{request_id}/ 内执行
- 子进程 + 超时（默认 120s）
- 不允许 import 网络库（用 ast 简单检查 import 黑名单）
- 输出文件强制重命名为 output.<ext>
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

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

# PyInstaller bundles do not contain a standalone Python interpreter.  In a
# frozen process ``sys.executable`` points back to the EchoDesk backend binary,
# so generated scripts must be routed through the hidden worker implemented by
# ``backend/packaging/entrypoint.py`` instead of being passed to the ordinary
# backend CLI as if the executable were ``python``.
PACKAGED_PYTHON_WORKER_FLAG = "--echodesk-python-worker"
_DIAGNOSTIC_SCHEMA_VERSION = 1
_DIAGNOSTIC_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_logger = logging.getLogger(__name__)


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


def _python_process_argv(script_path: Path) -> list[str]:
    """Return the interpreter/worker argv for one generated Python script."""

    resolved_script = script_path.resolve(strict=True)
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            PACKAGED_PYTHON_WORKER_FLAG,
            str(resolved_script),
        ]
    return [sys.executable, str(resolved_script)]


def _diagnostic_directory(
    diagnostic_dir: Path,
    *,
    artifact_id: str,
) -> tuple[str, Path]:
    """Create one owner-private durable directory for a failed Python run."""

    root = diagnostic_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_id = _DIAGNOSTIC_ID_RE.sub("_", artifact_id).strip("._") or "artifact"
    diagnostic_id = f"{safe_id}-{uuid4().hex[:12]}"
    directory = root / diagnostic_id
    directory.mkdir(mode=0o700)
    return diagnostic_id, directory


def _write_diagnostic_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_diagnostic_json(path: Path, value: Mapping[str, object]) -> None:
    _write_diagnostic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _compact_error(stderr: str, *, limit: int = 1_600) -> str:
    """Keep both traceback header and final exception in the workflow error."""

    if len(stderr) <= limit:
        return stderr
    head = max(200, limit // 3)
    return f"{stderr[:head]}\n...[stderr truncated; see diagnostic log]...\n{stderr[-(limit - head - 49):]}"


def _persist_diagnostic(
    *,
    diagnostic_dir: Path | None,
    artifact_id: str,
    script_path: Path,
    code: str,
    expected_ext: str,
    stage: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    elapsed_s: float,
    timeout_s: float,
    output_path: Path,
    context: Mapping[str, object] | None,
) -> str | None:
    """Persist exact worker evidence without putting generated source in SQLite."""

    if diagnostic_dir is None:
        return None
    try:
        diagnostic_id, directory = _diagnostic_directory(
            diagnostic_dir,
            artifact_id=artifact_id,
        )
        # Keep the generated script only in the owner-private diagnostic root;
        # workflow error rows receive the opaque diagnostic_id, not user text.
        _write_diagnostic_text(directory / "script.py", code)
        _write_diagnostic_text(directory / "stdout.txt", stdout)
        _write_diagnostic_text(directory / "stderr.txt", stderr)
        metadata: dict[str, object] = {
            "schema_version": _DIAGNOSTIC_SCHEMA_VERSION,
            "diagnostic_id": diagnostic_id,
            "created_at": datetime.now(UTC).isoformat(),
            "artifact_id": artifact_id,
            "expected_ext": expected_ext,
            "stage": stage,
            "returncode": returncode,
            "elapsed_s": round(elapsed_s, 6),
            "timeout_s": timeout_s,
            "script_path": str(script_path),
            "script_bytes": len(code.encode("utf-8")),
            "script_lines": code.count("\n") + (1 if code else 0),
            "script_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
            "output_exists": output_path.exists(),
            "output_bytes": output_path.stat().st_size if output_path.exists() else 0,
            "context": dict(context or {}),
        }
        _write_diagnostic_json(directory / "metadata.json", metadata)
        _logger.warning(
            "skill diagnostic persisted: diagnostic_id=%s artifact_id=%s stage=%s path=%s",
            diagnostic_id,
            artifact_id,
            stage,
            directory,
        )
        return diagnostic_id
    except Exception as error:
        # A diagnostic failure must never hide the original worker failure.
        _logger.warning("skill diagnostic persistence failed: %s", type(error).__name__)
        return None


async def exec_python_to_artifact(
    code: str,
    build_dir: Path,
    *,
    expected_ext: str,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    diagnostic_dir: Path | None = None,
    diagnostic_context: Mapping[str, object] | None = None,
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

    # 重写 save()/output() 路径为绝对路径
    code_norm = re.sub(
        r"(doc|wb|workbook|pres|pdf)\.(save|output)\(\s*['\"][^'\"]+['\"]\s*\)",
        lambda match: f"{match.group(1)}.{match.group(2)}({str(output_path.resolve())!r})",
        code,
    )

    script_path = build_dir / "script.py"
    await asyncio.to_thread(script_path.write_text, code_norm, encoding="utf-8")

    artifact_id = str((diagnostic_context or {}).get("artifact_id") or build_dir.name)
    diagnostic_id: str | None = None

    try:
        ast.parse(code_norm, filename=str(script_path))
    except (SyntaxError, ValueError, TypeError) as error:
        stderr = f"{type(error).__name__}: {error}"
        diagnostic_id = _persist_diagnostic(
            diagnostic_dir=diagnostic_dir,
            artifact_id=artifact_id,
            script_path=script_path,
            code=code_norm,
            expected_ext=expected_ext,
            stage="syntax_validation",
            returncode=None,
            stdout="",
            stderr=stderr,
            elapsed_s=0.0,
            timeout_s=timeout_s,
            output_path=output_path,
            context=diagnostic_context,
        )
        suffix = f" diagnostic_id={diagnostic_id}" if diagnostic_id else ""
        return ExecResult(
            False,
            None,
            f"python syntax validation failed{suffix}: {stderr}",
            0.0,
        )

    subproc_env: dict[str, str] | None = None
    if env:
        subproc_env = {**os.environ, **dict(env)}

    t0 = time.monotonic()

    def _run() -> tuple[int, str, str]:
        proc = subprocess.run(
            _python_process_argv(script_path),
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=subproc_env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    try:
        rc, stdout, stderr = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        diagnostic_id = _persist_diagnostic(
            diagnostic_dir=diagnostic_dir,
            artifact_id=artifact_id,
            script_path=script_path,
            code=code_norm,
            expected_ext=expected_ext,
            stage="worker_timeout",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            elapsed_s=timeout_s,
            timeout_s=timeout_s,
            output_path=output_path,
            context=diagnostic_context,
        )
        suffix = f" diagnostic_id={diagnostic_id}" if diagnostic_id else ""
        return ExecResult(False, None, f"timeout after {timeout_s}s{suffix}: {stderr or e}", timeout_s)
    except Exception as e:  # pragma: no cover
        elapsed = time.monotonic() - t0
        diagnostic_id = _persist_diagnostic(
            diagnostic_dir=diagnostic_dir,
            artifact_id=artifact_id,
            script_path=script_path,
            code=code_norm,
            expected_ext=expected_ext,
            stage="worker_spawn",
            returncode=None,
            stdout="",
            stderr=f"{type(e).__name__}: {e}",
            elapsed_s=elapsed,
            timeout_s=timeout_s,
            output_path=output_path,
            context=diagnostic_context,
        )
        suffix = f" diagnostic_id={diagnostic_id}" if diagnostic_id else ""
        return ExecResult(False, None, f"{type(e).__name__}: {e}{suffix}", elapsed)

    elapsed = time.monotonic() - t0

    def _ok() -> bool:
        return output_path.exists() and output_path.stat().st_size > 100

    if rc == 0 and await asyncio.to_thread(_ok):
        return ExecResult(True, output_path, "", elapsed)

    diagnostic_id = _persist_diagnostic(
        diagnostic_dir=diagnostic_dir,
        artifact_id=artifact_id,
        script_path=script_path,
        code=code_norm,
        expected_ext=expected_ext,
        stage="worker_exit",
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        elapsed_s=elapsed,
        timeout_s=timeout_s,
        output_path=output_path,
        context=diagnostic_context,
    )
    suffix = f" diagnostic_id={diagnostic_id}" if diagnostic_id else ""
    return ExecResult(
        False,
        None,
        f"rc={rc}{suffix} stderr={_compact_error(stderr)} output_exists={output_path.exists()}",
        elapsed,
    )


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

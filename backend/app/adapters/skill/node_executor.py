"""Node.js 子进程执行器：跑 LLM 生成的 pptxgenjs 脚本。

设计要点：
- pptxgenjs 装在共享 prefix `{node_modules_root}/`（首次自动装），不在每个 build_dir 重复装
- 子进程 cwd=build_dir，但通过 NODE_PATH 找到共享 node_modules
- 超时 + 输出归一化为 build_dir/output.pptx
- import 黑名单 + 不允许 child_process / require('http(s)') / 网络写入（最小化沙箱）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_FORBIDDEN_NODE_TOKENS = (
    "require('child_process')",
    'require("child_process")',
    "child_process",
    "require('http')",
    'require("http")',
    "require('https')",
    'require("https")',
    "require('net')",
    'require("net")',
    "require('fs')",  # pptxgenjs 内部走 writeFile/saveAsync，无需用户脚本直接读文件
    'require("fs")',
    "process.exit",
    "eval(",
    "Function(",
)


def _is_safe_node(code: str) -> tuple[bool, str]:
    low = code.replace(" ", "")
    for tok in _FORBIDDEN_NODE_TOKENS:
        if tok.replace(" ", "") in low:
            return False, f"forbidden token: {tok}"
    return True, ""


@dataclass
class NodeExecResult:
    success: bool
    output_path: Path | None
    stderr: str
    elapsed_s: float


_NODE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LOCALAPPDATA",
    "APPDATA",
    "PATHEXT",
    "COMSPEC",
)


def node_runtime_environment(
    node_modules_root: Path,
    *,
    electron_runtime: bool,
) -> dict[str, str]:
    """构造最小跨平台 Node 环境；Electron runtime 只多一个显式模式开关。"""

    env = {key: os.environ[key] for key in _NODE_ENV_KEYS if os.environ.get(key)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", str(Path.home()))
    env["NODE_PATH"] = str((node_modules_root / "node_modules").resolve())
    if electron_runtime:
        env["ELECTRON_RUN_AS_NODE"] = "1"
    return env


def run_node_script(
    *,
    node_bin: str,
    script_path: Path,
    args: list[str],
    cwd: Path,
    node_modules_root: Path,
    electron_runtime: bool,
    timeout_s: float,
) -> tuple[int, str]:
    """以系统 Node 或 ``ELECTRON_RUN_AS_NODE`` 运行固定脚本。"""

    proc = subprocess.run(
        [node_bin, str(script_path), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=node_runtime_environment(
            node_modules_root,
            electron_runtime=electron_runtime,
        ),
        check=False,
    )
    return proc.returncode, proc.stderr or proc.stdout


async def ensure_pptxgenjs_installed(
    node_modules_root: Path,
    *,
    node_bin: str = "node",
    npm_bin: str = "npm",
    timeout_s: float = 240.0,
    allow_install: bool = True,
) -> tuple[bool, str]:
    """首次调用时把 pptxgenjs 装到 ``node_modules_root/node_modules/pptxgenjs``。

    后续调用如果已安装直接返回。

    安装包传 ``allow_install=False``，只接受构建期已经收进包内的固定依赖；
    源码环境仍可用 PATH 中的 node/npm 首次安装 legacy 依赖。
    """
    pptx_dir = node_modules_root / "node_modules" / "pptxgenjs"
    if pptx_dir.exists():
        return True, "cached"

    if not allow_install:
        return False, f"preinstalled pptxgenjs missing: {pptx_dir}"

    missing = [b for b in (node_bin, npm_bin) if not _is_executable(b)]
    if missing:
        return False, f"node/npm not executable: {', '.join(missing)}"

    await asyncio.to_thread(node_modules_root.mkdir, parents=True, exist_ok=True)
    pkg_json = node_modules_root / "package.json"
    if not pkg_json.exists():
        await asyncio.to_thread(
            pkg_json.write_text,
            json.dumps(
                {
                    "name": "echo-skill-node-deps",
                    "version": "1.0.0",
                    "private": True,
                    "dependencies": {"pptxgenjs": "^3.12.0"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _install() -> tuple[int, str]:
        proc = subprocess.run(
            [npm_bin, "install", "--no-audit", "--no-fund", "--loglevel=error", "pptxgenjs"],
            cwd=str(node_modules_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, (proc.stderr or proc.stdout)[-800:]

    try:
        rc, msg = await asyncio.to_thread(_install)
    except subprocess.TimeoutExpired as e:
        return False, f"npm install timeout: {e}"
    except FileNotFoundError as e:
        return False, f"node/npm not on PATH: {e}"

    installed = rc == 0 and pptx_dir.exists()
    return (installed, "installed" if installed else f"npm rc={rc}: {msg}")


async def exec_node_to_artifact(
    code: str,
    build_dir: Path,
    *,
    node_modules_root: Path,
    expected_ext: str = "pptx",
    node_bin: str = "node",
    npm_bin: str = "npm",
    timeout_s: float = 180.0,
    electron_runtime: bool = False,
    allow_install: bool = True,
) -> NodeExecResult:
    """执行 pptxgenjs 脚本，产物归一到 ``build_dir/output.pptx``。"""
    ok, reason = _is_safe_node(code)
    if not ok:
        return NodeExecResult(False, None, reason, 0.0)

    ok, msg = await ensure_pptxgenjs_installed(
        node_modules_root,
        node_bin=node_bin,
        npm_bin=npm_bin,
        allow_install=allow_install,
    )
    if not ok:
        return NodeExecResult(False, None, f"pptxgenjs install failed: {msg}", 0.0)

    await asyncio.to_thread(build_dir.mkdir, parents=True, exist_ok=True)
    output_path = build_dir / f"output.{expected_ext}"

    # 把 writeFile 的产物名归一为 output.<ext>
    safe_path = str(output_path.resolve()).replace("\\", "\\\\").replace("'", "\\'")
    js_quoted = f"'{safe_path}'"
    code_norm = re.sub(
        r"(writeFile|writeFileSync)\s*\(\s*\{\s*fileName\s*:\s*['\"][^'\"]+['\"]",
        f"\\1({{ fileName: {js_quoted}",
        code,
    )
    # 兼容 saveAsync({ fileName: 'x.pptx' })
    code_norm = re.sub(
        r"saveAsync\s*\(\s*\{\s*fileName\s*:\s*['\"][^'\"]+['\"]",
        f"saveAsync({{ fileName: {js_quoted}",
        code_norm,
    )

    script_path = build_dir / "slides.js"
    await asyncio.to_thread(script_path.write_text, code_norm, encoding="utf-8")

    t0 = time.monotonic()

    def _run() -> tuple[int, str]:
        return run_node_script(
            node_bin=node_bin,
            script_path=script_path,
            args=[],
            cwd=build_dir,
            node_modules_root=node_modules_root,
            electron_runtime=electron_runtime,
            timeout_s=timeout_s,
        )

    try:
        rc, stderr = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as e:
        return NodeExecResult(False, None, f"timeout after {timeout_s}s: {e}", timeout_s)
    except FileNotFoundError as e:
        return NodeExecResult(False, None, f"node not on PATH: {e}", 0.0)

    elapsed = time.monotonic() - t0

    def _ok() -> bool:
        return output_path.exists() and output_path.stat().st_size > 2000

    if rc == 0 and await asyncio.to_thread(_ok):
        return NodeExecResult(True, output_path, "", elapsed)
    return NodeExecResult(False, None, f"rc={rc} stderr={stderr[:600]}", elapsed)


def _is_executable(bin_path: str) -> bool:
    """检查 bin 是绝对路径并且文件存在/可执行，或者能通过 PATH 找到。"""
    if "/" in bin_path or "\\" in bin_path:
        return os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)
    return shutil.which(bin_path) is not None

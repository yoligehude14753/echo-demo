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
import re
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


async def ensure_pptxgenjs_installed(
    node_modules_root: Path,
    *,
    node_bin: str = "node",
    npm_bin: str = "npm",
    timeout_s: float = 240.0,
) -> tuple[bool, str]:
    """首次调用时把 pptxgenjs 装到 ``node_modules_root/node_modules/pptxgenjs``。

    后续调用如果已安装直接返回。

    前置检查：node_bin 不可执行时立刻返回失败，避免做无意义的 ~80MB npm install。
    """
    missing = [b for b in (node_bin, npm_bin) if not _is_executable(b)]
    if missing:
        return False, f"node/npm not executable: {', '.join(missing)}"

    await asyncio.to_thread(node_modules_root.mkdir, parents=True, exist_ok=True)
    pptx_dir = node_modules_root / "node_modules" / "pptxgenjs"
    if pptx_dir.exists():
        return True, "cached"

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

    if rc != 0 or not pptx_dir.exists():
        return False, f"npm rc={rc}: {msg}"
    return True, "installed"


async def exec_node_to_artifact(
    code: str,
    build_dir: Path,
    *,
    node_modules_root: Path,
    expected_ext: str = "pptx",
    node_bin: str = "node",
    npm_bin: str = "npm",
    timeout_s: float = 180.0,
) -> NodeExecResult:
    """执行 pptxgenjs 脚本，产物归一到 ``build_dir/output.pptx``。"""
    ok, reason = _is_safe_node(code)
    if not ok:
        return NodeExecResult(False, None, reason, 0.0)

    ok, msg = await ensure_pptxgenjs_installed(
        node_modules_root, node_bin=node_bin, npm_bin=npm_bin
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

    nm_path = str((node_modules_root / "node_modules").resolve())
    t0 = time.monotonic()

    def _run() -> tuple[int, str]:
        proc = subprocess.run(
            [node_bin, str(script_path)],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={"NODE_PATH": nm_path, "PATH": _path_env(), "HOME": str(Path.home())},
            check=False,
        )
        return proc.returncode, proc.stderr or proc.stdout

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


def _node_search_paths() -> list[str]:
    """返回 node/npm 可能存在的所有目录，覆盖 Electron 沙箱 + 终端环境两种情况。

    Electron 通过 launchctl/open 启动时，子进程 PATH 通常只有
    ``/usr/bin:/bin:/usr/sbin:/sbin``，homebrew/fnm/nvm/volta 全不在里面。
    这里枚举所有常见安装前缀，保证在两种环境下都能找到 node。
    """
    import glob
    import os

    candidates: list[str] = []

    # 1. 当前进程已有的 PATH（终端直接跑时有效）
    current = os.environ.get("PATH", "")
    if current:
        candidates.extend(current.split(":"))

    home = str(Path.home())

    # 2. Homebrew（Apple Silicon + Intel）
    candidates += ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]

    # 3. fnm（Fast Node Manager）：版本 default alias 的 bin 目录
    for fnm_root in [
        f"{home}/.local/share/fnm",
        f"{home}/.fnm",
    ]:
        for alias_link in glob.glob(f"{fnm_root}/aliases/default"):
            try:
                resolved = Path(alias_link).resolve()
                candidates.append(str(resolved / "bin"))
            except OSError:
                pass
        # 遍历已装版本，选最新的
        for ver_bin in sorted(glob.glob(f"{fnm_root}/node-versions/*/installation/bin"), reverse=True):
            candidates.append(ver_bin)

    # 4. nvm
    for nvm_root in [f"{home}/.nvm", f"{home}/.config/nvm"]:
        for ver_bin in sorted(glob.glob(f"{nvm_root}/versions/node/*/bin"), reverse=True):
            candidates.append(ver_bin)

    # 5. volta
    candidates.append(f"{home}/.volta/bin")

    # 6. n / asdf
    candidates += [f"{home}/.n/bin", f"{home}/.asdf/shims"]

    # 系统兜底
    candidates += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

    # 去重，保留顺序
    seen: set[str] = set()
    result: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _path_env() -> str:
    return ":".join(_node_search_paths())


def _is_executable(bin_path: str) -> bool:
    """检查 bin 是绝对路径并且文件存在/可执行，或者能通过扩展 PATH 找到。"""
    import os
    import shutil as _shutil

    if "/" in bin_path or "\\" in bin_path:
        return os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)
    # 先用扩展 PATH 搜，再 fallback 到系统 shutil.which
    found = _shutil.which(bin_path, path=_path_env())
    if found:
        return True
    return _shutil.which(bin_path) is not None

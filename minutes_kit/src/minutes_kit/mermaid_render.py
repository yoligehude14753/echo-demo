"""Mermaid 源码 → PNG（用于 docx 嵌入）。

实现：
- 首选 mmdc CLI（``@mermaid-js/mermaid-cli``），离线
- 备选 mermaid.ink HTTP 服务（在线，无需本地依赖；但有 URL 长度限制）
- 都不可用时返回 None，上层用文字占位

依赖检测：
- ``shutil.which("mmdc")`` 检查本机是否装了 mmdc
- 在线兜底走 ``httpx`` GET ``https://mermaid.ink/img/<base64>``
"""
from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

from loguru import logger


async def render_mermaid_to_png(
    mermaid_src: str,
    out_path: Path,
    *,
    timeout_s: float = 12.0,
    width: int = 1600,
    background: str = "white",
    allow_online_fallback: bool = True,
) -> Path | None:
    """渲染 Mermaid 源码到 PNG 文件。

    Args:
        mermaid_src: Mermaid 源码
        out_path: PNG 输出路径
        timeout_s: 总超时（包含两条路径的）
        width: 渲染宽度
        background: "transparent" / "white" / "#fff" 等
        allow_online_fallback: 本地 mmdc 失败时是否调 mermaid.ink

    Returns:
        out_path 若成功；None 若全部失败
    """
    if not mermaid_src.strip():
        logger.warning("[mermaid_render] 空源码，跳过")
        return None

    # 路径 1：本机 mmdc
    if shutil.which("mmdc"):
        ok = await _render_via_mmdc(mermaid_src, out_path, timeout_s, width, background)
        if ok:
            return out_path
        logger.warning("[mermaid_render] mmdc 本机渲染失败，尝试在线 fallback")
    else:
        logger.info("[mermaid_render] 本机无 mmdc binary，使用在线 fallback")

    # 路径 2：mermaid.ink
    if allow_online_fallback:
        ok = await _render_via_mermaid_ink(mermaid_src, out_path, timeout_s)
        if ok:
            return out_path

    return None


async def _render_via_mmdc(
    src: str,
    out_path: Path,
    timeout_s: float,
    width: int,
    background: str,
) -> bool:
    """用 mmdc subprocess 渲染。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src_file = out_path.with_suffix(".mmd")
    try:
        src_file.write_text(src, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            "mmdc",
            "-i",
            str(src_file),
            "-o",
            str(out_path),
            "-b",
            background,
            "-w",
            str(width),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"[mermaid_render] mmdc 超时 ({timeout_s}s)")
            return False

        if proc.returncode != 0:
            logger.warning(
                f"[mermaid_render] mmdc 失败 rc={proc.returncode} "
                f"stderr={stderr.decode('utf-8', errors='replace')[:300]}"
            )
            return False

        if not out_path.exists() or out_path.stat().st_size < 100:
            logger.warning("[mermaid_render] mmdc 退出码 0 但产物缺失或过小")
            return False

        return True

    except FileNotFoundError:
        logger.warning("[mermaid_render] mmdc binary 不存在")
        return False
    except Exception as exc:
        logger.warning(f"[mermaid_render] mmdc 异常: {exc}")
        return False
    finally:
        try:
            src_file.unlink(missing_ok=True)
        except Exception:
            pass


async def _render_via_mermaid_ink(
    src: str,
    out_path: Path,
    timeout_s: float,
) -> bool:
    """用 mermaid.ink 在线服务渲染。

    协议：GET https://mermaid.ink/img/<base64(src)>?type=png&bgColor=FFFFFF
    """
    try:
        import httpx
    except ImportError:
        logger.warning("[mermaid_render] httpx 未安装，无法走在线 fallback")
        return False

    try:
        encoded = base64.urlsafe_b64encode(src.encode("utf-8")).decode("ascii").rstrip("=")
        url = f"https://mermaid.ink/img/{encoded}?type=png&bgColor=FFFFFF"
        # URL 长度上限~8KB，超出 mermaid.ink 会 414
        if len(url) > 7500:
            logger.warning(f"[mermaid_render] mermaid 源码过长 ({len(url)} chars URL)，跳过在线 fallback")
            return False

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(
                    f"[mermaid_render] mermaid.ink 返回 {resp.status_code}: {resp.text[:200]}"
                )
                return False
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(resp.content)
            return True
    except Exception as exc:
        logger.warning(f"[mermaid_render] mermaid.ink 在线渲染异常: {exc}")
        return False

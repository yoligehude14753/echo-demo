"""HTML 渲染器：MeetingMinutesData → 单文件 preview.html。

特性：
- Jinja2 模板 + 内联 CSS / mermaid.js（双击离线可看）
- 暗色模式自动跟随 prefers-color-scheme
- Markdown → HTML 用极简 line-based 转换（不引 markdown lib，依赖最小）
- 模板渲染失败时降级用 _fallback_office.fallback_html_minimal
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from minutes_kit._fallback_office import fallback_html_minimal
from minutes_kit.models import MeetingMinutesData

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


_FLOW_KIND_LABEL = {
    "flowchart": "决策链路",
    "sequenceDiagram": "对话时序",
    "mindmap": "话题脑图",
    "timeline": "时间线",
}

# 三栏卡片视觉装饰：emoji + tint + tag class/label 轮换
_CARD_DECOR = [
    {"emoji": "🎯", "tint": "tint-orange", "tag_class": "tag-warn",    "tag_label": "重点决议"},
    {"emoji": "📌", "tint": "tint-blue",   "tag_class": "tag-neutral", "tag_label": "已确认"},
    {"emoji": "✅", "tint": "tint-green",  "tag_class": "tag-success", "tag_label": "推进中"},
]


def render_html(
    data: MeetingMinutesData,
    out_path: Path,
    *,
    inline_mermaid_js: bool = True,
) -> Path:
    """渲染并写入 preview.html。"""
    try:
        template = _env.get_template("minutes.html.j2")
        rendered = template.render(
            data=data,
            time_display=_format_time_display(data.from_time, data.to_time),
            summary_html=_markdown_to_html(data.summary_md),
            mermaid_js=_load_mermaid_js() if inline_mermaid_js else _mermaid_cdn_loader(),
            core_points=_extract_core_points(data),
            decision_groups=_group_decisions(data),
            flow_kind_label=_FLOW_KIND_LABEL.get(data.flow_kind, data.flow_kind),
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        logger.info(
            f"[html] rendered {out_path.name} "
            f"({len(rendered):,} chars, mermaid={'inline' if inline_mermaid_js else 'cdn'})"
        )
        return out_path
    except Exception as exc:
        logger.warning(f"[html] Jinja2 渲染失败，使用极简兜底: {exc}")
        fallback_html_minimal(
            target_path=out_path,
            title=data.title,
            abstract=data.abstract,
            summary_md=data.summary_md,
            decisions=[d.to_dict() for d in data.decisions],
            todos=[t.to_dict() for t in data.todos],
        )
        return out_path


# ── 辅助：核心结论 / 装饰 ─────────────────────────────────────────────────


def _extract_core_points(data: MeetingMinutesData) -> list[str]:
    """从决议中挑 ≤3 条作为「核心结论」。

    返回值是 **HTML 字符串**（含 markdown bold 已转为 <strong>），
    模板里用 ``| safe`` 渲染。
    """
    raw: list[str] = []
    seen: set[str] = set()
    for d in data.decisions:
        stmt = d.statement.strip()
        if stmt and stmt not in seen:
            seen.add(stmt)
            raw.append(stmt)
        if len(raw) >= 3:
            break

    if not raw and data.topics:
        for tp in data.topics[:3]:
            for kp in tp.key_points[:1]:
                if kp.strip():
                    raw.append(kp.strip())
                    break
            if len(raw) >= 3:
                break

    return [_inline_md_bold(s) for s in raw]


def _group_decisions(data: MeetingMinutesData) -> list[dict]:
    """把 N 条决议聚成最多 3 张卡片。

    输出 schema:
      {
        title: str,                # 卡片小标题
        emoji: str,                # 装饰 emoji
        tint: str,                 # CSS class (tint-orange/blue/green)
        tag_class: str,            # CSS class
        tag_label: str,            # 徽章文本
        bullets: list[str],        # 已转 HTML 的 bullet 列表（含加粗）
      }

    分组规则（renderer 端简单切分；后续可让 LLM 直接输出主题分组）：
      - N == 0 → []
      - N <= 3 → 每条决议一张卡
      - N >  3 → 平均切 3 组（向上取整）
    """
    decs = data.decisions
    if not decs:
        return []

    chunks = _balance_chunks(list(decs), 3)

    groups: list[dict] = []
    for i, chunk in enumerate(chunks):
        decor = _CARD_DECOR[i % 3]
        head = chunk[0]
        # 卡标题：第一条决议（去掉 markdown 加粗标记，避免 ** 出现在标题）
        title = _card_title_from_statement(head.statement)
        # bullets：每条决议拼成一行，关键词加粗；多条 statement 时每条一行
        bullets = []
        for d in chunk:
            bullets.append(_decision_to_bullet_html(d))
        groups.append(
            {
                "title": title,
                "emoji": decor["emoji"],
                "tint": decor["tint"],
                "tag_class": decor["tag_class"],
                "tag_label": decor["tag_label"],
                "bullets": bullets,
            }
        )
    return groups


def _strip_md_bold(s: str) -> str:
    """剥掉 markdown ** 加粗标记，仅保留文本。"""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", s)


def _card_title_from_statement(statement: str) -> str:
    """卡片标题：优先取 statement 中第一个 **加粗** 短语；
    如果没有，取 statement 的前 10 字。
    """
    m = re.search(r"\*\*(.+?)\*\*", statement)
    if m:
        return _truncate(m.group(1).strip(), 16)
    return _truncate(_strip_md_bold(statement).strip(), 12)


def _decision_to_bullet_html(d) -> str:
    """单条决议 → bullet HTML。

    保留 statement 的 **markdown 加粗**；rationale/impact 不在三栏卡片里展示
    （参考图风格：卡片仅承载关键事实 bullet，背景信息留给「议题展开」）。
    """
    return _inline_md_bold(d.statement)


def _inline_md_bold(s: str) -> str:
    """支持 **加粗** 的极简 markdown，先转义其余 HTML 字符。"""
    if not s:
        return ""
    escaped = _escape(s)
    # **bold** → <strong>bold</strong>；非贪心
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _balance_chunks(items: list, k: int) -> list[list]:
    """把 items 平均切成 k 个非空 chunk，前面的 chunk 多分。"""
    n = len(items)
    if n == 0 or k == 0:
        return []
    base, rem = divmod(n, k)
    chunks: list[list] = []
    idx = 0
    for i in range(k):
        size = base + (1 if i < rem else 0)
        if size == 0:
            continue
        chunks.append(items[idx : idx + size])
        idx += size
    return chunks


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 辅助：时间显示 ─────────────────────────────────────────────────────────


def _format_time_display(from_time: str, to_time: str) -> str:
    """把 ISO8601 / HH:MM:SS 渲染为友好显示串。"""

    def _parse(ts: str) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    f = _parse(from_time)
    t = _parse(to_time)
    if f and t:
        if f.date() == t.date():
            return f"{f.strftime('%Y-%m-%d %H:%M')} - {t.strftime('%H:%M')}"
        return f"{f.strftime('%Y-%m-%d %H:%M')} → {t.strftime('%Y-%m-%d %H:%M')}"
    return f"{from_time} - {to_time}".strip(" -")


# ── 辅助：极简 Markdown → HTML ───────────────────────────────────────────


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HTML_ESCAPE_RE = re.compile(r"[&<>\"']")
_HTML_ESCAPE_MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}


def _escape(s: str) -> str:
    return _HTML_ESCAPE_RE.sub(lambda m: _HTML_ESCAPE_MAP[m.group(0)], s)


def _inline_md(s: str) -> str:
    """转义 + 处理 **bold** / `code` / [text](url)。"""
    s = _escape(s)
    s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _CODE_RE.sub(r"<code>\1</code>", s)
    s = _LINK_RE.sub(r'<a href="\2">\1</a>', s)
    return s


def _markdown_to_html(md: str) -> str:
    """极简 markdown 渲染：支持 # / ## / ### / 嵌套 - / 1. / **bold** / `code` / 段落。

    嵌套规则：以行首空格数判定层级（2 空格 = 1 级缩进）。
    """
    if not md or not md.strip():
        return ""

    # ── 第一步：把每行解析成 token ──
    tokens: list[tuple[str, int, str]] = []  # (kind, level, text/tag)
    for raw_line in md.split("\n"):
        if not raw_line.strip():
            tokens.append(("blank", 0, ""))
            continue
        leading = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        level = leading // 2

        matched = False
        for prefix, tag in (("#### ", "h4"), ("### ", "h3"), ("## ", "h2"), ("# ", "h1")):
            if stripped.startswith(prefix):
                tokens.append(("heading", 0, f"<{tag}>{_inline_md(stripped[len(prefix):].strip())}</{tag}>"))
                matched = True
                break
        if matched:
            continue

        m_b = re.match(r"^([-*])\s+(.+)$", stripped)
        m_n = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m_b:
            tokens.append(("ul", level, _inline_md(m_b.group(2).strip())))
        elif m_n:
            tokens.append(("ol", level, _inline_md(m_n.group(2).strip())))
        else:
            tokens.append(("p", 0, _inline_md(stripped)))

    # ── 第二步：基于 token 流构建 HTML ──
    out: list[str] = []
    stack: list[str] = []  # 当前打开的 list tag 栈
    paragraph_buf: list[str] = []

    def flush_p():
        if paragraph_buf:
            out.append(f"<p>{' '.join(paragraph_buf)}</p>")
            paragraph_buf.clear()

    def close_all_lists():
        while stack:
            out.append(f"</{stack.pop()}>")

    i = 0
    while i < len(tokens):
        kind, level, text = tokens[i]
        if kind == "blank":
            flush_p()
            i += 1
            continue
        if kind == "heading":
            flush_p()
            close_all_lists()
            out.append(text)
            i += 1
            continue
        if kind == "p":
            close_all_lists()
            paragraph_buf.append(text)
            i += 1
            continue
        # list item
        flush_p()
        # 调整栈到与目标 level + tag 匹配
        target_depth = level + 1
        # 关闭多余层
        while len(stack) > target_depth:
            out.append(f"</{stack.pop()}>")
        # 同深度但 tag 不同 → 关闭最里层
        if len(stack) == target_depth and stack[-1] != kind:
            out.append(f"</{stack.pop()}>")
        # 还需要打开
        while len(stack) < target_depth:
            out.append(f"<{kind}>")
            stack.append(kind)
        out.append(f"<li>{text}</li>")
        i += 1

    flush_p()
    close_all_lists()
    return "\n".join(out)


# ── 辅助：mermaid.js 加载策略 ────────────────────────────────────────────


def _load_mermaid_js() -> str:
    """读 static/mermaid.min.js；不存在时返回 CDN loader 字符串。"""
    p = _STATIC_DIR / "mermaid.min.js"
    if p.is_file() and p.stat().st_size > 1000:
        try:
            return p.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[html] 读取 mermaid.min.js 失败: {exc}")
    return _mermaid_cdn_loader()


def _mermaid_cdn_loader() -> str:
    """当 static/mermaid.min.js 不存在时的兜底：动态加载 CDN。"""
    return (
        "(function(){"
        "var s=document.createElement('script');"
        "s.src='https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js';"
        "s.onload=function(){"
        "  if(window.mermaid){"
        "    var d=window.matchMedia('(prefers-color-scheme: dark)').matches;"
        "    window.mermaid.initialize({startOnLoad:true,theme:d?'dark':'default',securityLevel:'loose'});"
        "  }"
        "};"
        "document.head.appendChild(s);"
        "})();"
    )

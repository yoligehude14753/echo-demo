# 接入 echo backend 指南（草案）

> 本模块当前不接入 echo backend。本文件描述**未来某个独立 milestone** 才会执行的接入步骤。
> 在本模块产物质量达标（见 [README §定位](README.md) 的验收清单）之前，**不要**执行下面任何步骤。

## 接入前提

- minutes_kit 自验通过：CLI + demo server 跑得通，sample 产物双击能看
- LLM Client 抽象稳定：`generate_minutes(transcript, llm_client, out_dir)` 签名不再调整
- HTML / docx 模板稳定：不再频繁返工

## 接入步骤（未来执行）

### Step 1：让 backend 把 minutes_kit 当 Python 包引用

`backend/pyproject.toml` 增 editable 依赖：

```toml
[tool.uv.sources]
minutes-kit = { path = "../minutes_kit", editable = true }

[project]
dependencies = [
    # ...原有依赖
    "minutes-kit",
]
```

### Step 2：写 EchoBridgeClient 适配 echo 现有 LLM

`minutes_kit/src/minutes_kit/llm_client.py::EchoBridgeClient` 当前是占位。
接入时实现为：

```python
class EchoBridgeClient:
    """包装 echo.app.llm.complete / complete_nano 走主模型 32B + ASR 噪声 hint。"""

    def __init__(self) -> None:
        # lazy import 避免循环
        from app.llm import complete, complete_nano  # type: ignore[import-not-found]
        from app.config import get_config            # type: ignore[import-not-found]
        self._complete = complete
        self._complete_nano = complete_nano
        self._cfg = get_config()

    async def complete(self, messages, *, model=None, max_tokens=16000):
        return await self._complete(
            messages=messages,
            tools=[],
            max_tokens=max_tokens,
            model=model or self._cfg.LLM_MAIN_MODEL,
        )

    async def complete_with_schema(self, messages, schema, *, model=None, temperature=0.2):
        raw = await self._complete_nano(
            messages=messages, response_format=schema, temperature=temperature
        )
        import json
        return json.loads(raw)
```

### Step 3：把 transcript 数据投影成 minutes_kit 输入

`backend/app/meeting/` 已经有 `MeetingSession.turns` 数据结构。写一个适配函数：

```python
# backend/app/meeting/minutes_bridge.py
from minutes_kit.models import TranscriptTurn

def from_echo_session(session) -> list[TranscriptTurn]:
    return [
        TranscriptTurn(speaker=t.speaker or "?", text=t.text, ts=t.ts.isoformat())
        for t in session.turns
        if t.text and t.text.strip()
    ]
```

### Step 4：新增 API 端点

`backend/app/api/meeting.py` 新增（不替换旧端点）：

```python
@router.post("/{device_id}/minutes_v2")
async def generate_minutes_v2(device_id: str, req: NotesRequest):
    # 1. 取 transcript（沿用 _llm_generate_notes 同样的数据源逻辑）
    turns = await _load_turns(device_id, req.duration_minutes)
    # 2. 调 minutes_kit
    from minutes_kit import generate_minutes
    from minutes_kit.llm_client import EchoBridgeClient
    artifact_dir = Path(get_config().ARTIFACTS_DIR) / "meeting" / minutes_id
    result = await generate_minutes(
        transcript=turns,
        llm_client=EchoBridgeClient(),
        out_dir=artifact_dir,
        title_hint=req.title,
        participants=...,
    )
    # 3. 落 meeting_minutes_v2 表 + 返回 URL
    return {"minutes_id": minutes_id, ..., "preview_url": ..., "docx_url": ...}
```

### Step 5：挂 StaticFiles 提供 artifacts

`backend/app/main.py` 增：

```python
artifacts_dir = Path(get_config().ARTIFACTS_DIR)
artifacts_dir.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=str(artifacts_dir)), name="artifacts")
```

### Step 6：desktop 端 iframe 预览组件

新增 `desktop/src/components/MeetingMinutesPreview.tsx`：

```tsx
<iframe src={`${httpBase}/artifacts/meeting/${id}/preview.html`} />
```

工具栏按钮调 `window.echo.system.openExternal(docx_url)` 下载/打开 Word。

## 不要在接入时做的事

- 不要把 minutes_kit 代码搬进 `backend/app/`（保持独立可演进）
- 不要让 minutes_kit import `backend.app.*`（永远反向）
- 不要让旧 `/notes` 端点引用 minutes_kit（让两套并行，慢慢迁）
- 不要在接入第一版同时升级模板视觉（一次只改一件事）

## 验收（接入完成判定）

- 旧 `/notes` 端点行为不变
- 新 `/minutes_v2` 端点产出 minutes_kit 4 件套
- 桌面端能预览 HTML + 下载 docx
- minutes_kit 自验 CLI 仍然能跑（接入没破坏独立性）

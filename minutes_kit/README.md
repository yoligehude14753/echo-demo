# minutes_kit

> **这是 echo 仓库内的独立子模块，与 `backend/app/meeting/` 实时摘要 Agent 无关。**
> 本模块专注「离线会议纪要产物精修」——输入是已完成的会议转录，输出是双击就能验收的
> HTML 预览 + Word 文档 + Mermaid 流程图。本模块处于 incubation 状态：
> **不被 `backend.app.*` 引用，也不引用 `backend.app.*`**；产物质量达标后才会由独立 milestone
> 接入 echo 主线。

## 定位

- **不是什么**
  - 不是 `backend/app/meeting/summarizer.py`（那是实时主题边界 + 阶段总结的运行时 Agent）
  - 不是 echo 后端的 API
  - 不是要替换 `backend/app/api/meeting.py` 现有端点
- **是什么**
  - 一个**独立的 Python 包** + CLI + demo server
  - 输入：transcript（文本或 JSON）
  - 输出：`data.json` / `preview.html` / `minutes.docx` / `flow.png` 四件套
  - 自带 sample 转录 + 黄金对照产物，闭环可验证

## 快速开始

### 安装

```bash
cd ~/Desktop/all/echo/minutes_kit
python -m venv .venv
source .venv/bin/activate
pip install -e .[demo,dev]
```

### CLI 一行跑通

```bash
export OPENAI_API_KEY=sk-...
# 或指向 m27 proxy：
# export OPENAI_BASE_URL=http://127.0.0.1:4127/v1
# export OPENAI_API_KEY=dummy

python -m minutes_kit.cli \
  --transcript demo/sample_transcripts/meetly_demo.txt \
  --out ./out/run_001/ \
  --participants "A,B,C" \
  --title-hint "周三例会"
```

跑完后双击 `out/run_001/preview.html` 看效果，双击 `out/run_001/minutes.docx` 看 Word 版本。

### demo server 自验

```bash
python -m minutes_kit.demo
# 浏览器打开 http://127.0.0.1:8810
```

页面上粘贴 transcript 文本 → 提交 → 直接看产物效果。

## 数据流

```
transcript.txt
   ↓
extractor.py (3 节点 LLM 编排)
   ├─ Node A: title + abstract + summary_md + topics
   ├─ Node B: decisions + todos
   └─ Node C: flow_mermaid + flow_kind
   ↓
MeetingMinutesData (正典 JSON)
   ├─→ renderers/html.py → preview.html (含 Mermaid 流程图)
   └─→ renderers/docx.py → minutes.docx
              ├─ 主路径: Claude Code + Anthropic docx skill
              └─ 兜底:   python-docx + Mermaid PNG 嵌入
```

## 依赖反转 / LLM Client

模块内部不 import 任何 echo 后端代码。LLM 调用通过 `LLMClient` Protocol 注入：

- **默认实现**：`OpenAIClient` — 用环境变量 `OPENAI_API_KEY` + `OPENAI_BASE_URL`，可指向真 OpenAI 也可指向 m27 proxy
- **接入用**：`EchoBridgeClient`（占位）— 未来 echo backend 接入时包装 `echo.app.llm.complete`

接入指南见 [INTEGRATION.md](INTEGRATION.md)。

## 边界声明（避免误解）

1. 本模块**不修改** echo 任何其他代码（`backend/` / `desktop/` / `firmware/` 全部冻结）
2. 本模块**不进入** echo backend 的 `pyproject.toml` / `requirements.txt`
3. 本模块**不进入** echo CI / pre-commit hooks
4. 本模块**不写入** echo root `README.md` / `ARCHITECTURE.md`
5. 本模块**不为自身添加** `.cursor/rules/` 条目
6. 本模块**不 import** `backend.app.*`，也**不被** `backend.app.*` import（本次范围内）

## 目录结构

```
minutes_kit/
├── README.md                 # 本文件
├── INTEGRATION.md            # 未来接入 echo backend 的指南
├── pyproject.toml            # 独立依赖
├── src/minutes_kit/          # Python 包
│   ├── models.py             # MeetingMinutesData / Decision / Todo / Topic
│   ├── llm_client.py         # LLMClient Protocol + 实现
│   ├── extractor.py          # 3 节点 LLM 编排
│   ├── renderers/            # HTML / docx 双渲染器
│   ├── mermaid_render.py     # mmdc CLI 调用
│   ├── _claude_*.py          # 移植自 meetly
│   ├── _fallback_office.py   # 移植自 meetly
│   ├── templates/            # Jinja2 模板
│   └── static/               # mermaid.min.js
├── cli.py                    # 命令行入口
├── demo/                     # demo dev server + 样例转录
└── tests/                    # 单测 + 集成测试
```

## 关于流程图

LLM 自由选 4 种 Mermaid 图种之一：

- `flowchart TD` — 决策因果链路（默认）
- `sequenceDiagram` — 说话人交互时序
- `mindmap` — 话题分层结构
- `timeline` — 时序事件

约束（写在 LLM system prompt 里）：
- 节点 ID camelCase 无空格
- 节点数 3-12
- 禁用 `style` / `click` / `subgraph` / HTML 实体

## 来源说明

- HTML 模板设计基线借鉴 `~/Desktop/all/meetly/workspaces/.../会议报告.html`
- Claude Code subprocess 封装移植自 `~/Desktop/all/meetly/apps/server/app/tools/_claude_code*.py`
- python-docx 兜底移植自 `~/Desktop/all/meetly/apps/server/app/tools/_fallback_office.py`
- LLM prompt 体感借鉴 `~/Desktop/all/echo/backend/app/api/meeting.py::_llm_generate_notes`

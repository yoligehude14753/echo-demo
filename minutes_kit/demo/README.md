# demo dev server

跑这个 server 是为了**人工验证产物质量**：粘贴一段 transcript，看产物视觉、表格、流程图。

## 启动

```bash
cd ~/Desktop/all/echo/minutes_kit
source .venv/bin/activate         # 或者 uv venv 等
pip install -e .[demo,dev]

# 配置 LLM（任选其一）
export OPENAI_API_KEY=sk-...
# 或指向 echo m27 proxy 等 OpenAI 兼容服务：
# export OPENAI_BASE_URL=http://127.0.0.1:4127/v1
# export OPENAI_API_KEY=dummy
# export MINUTES_KIT_MODEL=MiniMax-M2.7

python demo/server.py
```

浏览器打开 [http://127.0.0.1:8810](http://127.0.0.1:8810)，粘贴 transcript → 提交 → 看产物。

## 端口与隔离

- 默认 `127.0.0.1:8810`，跟 echo backend 完全无关
- 改端口：`MINUTES_KIT_DEMO_HOST=0.0.0.0 MINUTES_KIT_DEMO_PORT=9001 python demo/server.py`

## 产物存储

- `minutes_kit/out/demo_runs/<run_id>/`
- preview.html / minutes.docx / data.json / flow.png 一份齐全
- 已加进 `.gitignore`，不会提交

## 跟主流程的区别

- demo server 没有持久化 DB，刷新后历史仅靠扫描 `out/demo_runs/`
- 没有权限/多租户/鉴权
- 只为打磨 prompt + 模板视觉
- 接入 echo 时这一切都重新设计（见根目录 INTEGRATION.md）

## 示例转录

`sample_transcripts/` 下有几份样例，可以直接打开复制内容粘到 server 表单里。

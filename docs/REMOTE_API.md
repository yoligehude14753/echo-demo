# 远程后端：eight Endpoint

> Current (2026-06-18): EchoDesk demo 的 STT / TTS / Fast LLM 已迁到 eight (`100.76.3.59`)。

## 当前默认

| 服务 | 默认 URL | 模型 / 服务 |
|---|---|---|
| STT | `http://100.76.3.59:8090` | FireRedASR2-AED |
| Fast LLM | `http://100.76.3.59:7860/v1` | `qwen3.5-9b-local-gpu0` |
| TTS | `http://100.76.3.59:8094` | faster-qwen3-tts CustomVoice |

这些默认值已写入：

- `backend/app/config.py`
- `.env.example`
- 桌面端设置页 placeholder
- 本机 `~/.echodesk/config.json`

## 自检

```bash
tailscale status | grep eight
nc -zv 100.76.3.59 8090
nc -zv 100.76.3.59 8094
curl -m 5 http://100.76.3.59:7860/v1/models
```

期望：

- `8090` TCP 通，`/docs` 返回 HTTP 200。
- `8094` TCP 通；根路由 HTTP 404 也表示服务在线。
- `7860/v1/models` 返回 `qwen3.5-9b-local-gpu0`。

## 用户配置覆盖

EchoDesk 启动优先级是：

```text
env > ~/.echodesk/config.json > .env > code default
```

如果本机曾经写过旧地址，需要把 `~/.echodesk/config.json` 改成：

```json
{
  "stt_firered_url": "http://100.76.3.59:8090",
  "llm_fast_base_url": "http://100.76.3.59:7860/v1",
  "llm_fast_model": "qwen3.5-9b-local-gpu0",
  "tts_qwen3_url": "http://100.76.3.59:8094"
}
```

## 排障顺序

1. 看顶部 `eight` pill：绿表示 STT / TTS / Fast LLM 探针都通。
2. 看 `curl http://127.0.0.1:8769/healthz/full` 的 `remote` 字段。
3. 如果远场转写不清楚，先看 `/capture/stats` 的 `last_rms`、`last_speech_ratio`、`last_gate_reason`，区分麦克风输入太小、门控过滤、还是 STT 识别质量问题。
4. 导出诊断包，附带 `~/.echodesk/logs/backend.log` 和 capture stats。

## 历史说明

旧 heyi-bj / `*.yoliyoli.uk` cloudflared endpoint 是 2026-05-28 的历史方案，不再是 demo 默认路径。相关守护 SOP 仅保留在 `docs/ops/heyi-cloudflared-systemd.md` 作为历史归档。

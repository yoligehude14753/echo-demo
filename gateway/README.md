# echo-gateway

面向外部用户的 **OpenAI 兼容鉴权反向代理**。把 yunwu(主 LLM) + heyi-bj(fast LLM / STT / TTS) 统一收口到一个带 Bearer token 鉴权 + 限流的服务端网关。

## 为什么需要它

EchoDesk 客户端原本把 yunwu key 直接放在本地配置里。任何下发到客户端的密钥都能被逆向提取 —— 一旦泄露，yunwu 额度会被陌生人刷爆。

网关把真实凭证（yunwu key、heyi 地址）**只保存在服务端**。客户端只持有自己的 client token，调用网关；网关校验 token、限流后，用自己的真实凭证回源 yunwu/heyi。客户端永远拿不到真实密钥。

```
EchoDesk 客户端  --(Bearer client-token)-->  echo-gateway  --(真实凭证)-->  yunwu / heyi-bj
```

## 暴露的接口（全部 OpenAI 兼容）

| 路径 | 作用 | 上游路由 |
|------|------|----------|
| `GET /health` | 健康检查（无鉴权，不泄露上游信息） | — |
| `POST /v1/chat/completions` | 对话（支持 `stream`） | model ∈ `YUNWU_MODELS` → yunwu；否则 → heyi fast |
| `POST /v1/embeddings` | 向量化（RAG dense 检索，可选） | yunwu |
| `POST /v1/audio/transcriptions` | STT（multipart，file+language） | FireRed @ heyi-bj |
| `POST /v1/audio/speech` | TTS（json，input+voice） | Qwen3-TTS @ heyi-bj |

客户端的现有适配器（`openai_compatible.py` / `firered.py` / `qwen3_tts.py`）**无需改代码**，只要把 base_url 指向网关、key 换成 client token 即可。

## 鉴权 & 限流

- `Authorization: Bearer <client-token>`，token 必须在 `ECHO_GW_TOKENS` 白名单内，否则 401（fail-closed）。
- 每 token 滑动窗口限流：`RATE_LIMIT_WINDOW_S` 内最多 `RATE_LIMIT_MAX_REQUESTS` 次，超出 429。
- 单实例进程内限流足够；多实例水平扩展时需换 Redis。

生成 client token：

```bash
openssl rand -hex 24
```

## 本地运行

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # 填入真实 YUNWU_OPEN_KEY 与 client token
ECHO_GW_TOKENS=dev-tok .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
.venv/bin/pytest -q   # 15 项单测
```

## 部署到 heyi-bj（Docker）

heyi-bj 已经在跑 FireRed/Qwen3-TTS/fast-LLM，并有 Cloudflare 隧道 `*.yoliyoli.uk`。当前公版网关部署为：

- 公网地址：`https://echodesk.yoliyoli.uk`
- heyi-bj 本机端口：`8082`
- 容器内端口：`8080`
- 客户端鉴权：`Authorization: Bearer <client-token>`
- 未带 token / token 错误：`401`

浏览器直接打开根路径 `https://echodesk.yoliyoli.uk/` 返回 `{"detail":"Not Found"}` 是正常现象；该服务只提供 API。健康检查使用 `https://echodesk.yoliyoli.uk/health`。

```bash
# 在 heyi-bj 上
git clone <私有仓> && cd echo-demo/gateway
cp .env.example .env
# 编辑 .env：
#   ECHO_GW_TOKENS=<逗号分隔的 client token>
#   YUNWU_OPEN_KEY=<真实 yunwu key>
#   ECHO_GATEWAY_HOST_PORT=8082
#   HEYI_FAST_BASE_URL / HEYI_STT_BASE_URL / HEYI_TTS_BASE_URL
#     Docker bridge 模式访问宿主机时填：
#       http://host.docker.internal:7860/v1
#       http://host.docker.internal:8090
#       http://host.docker.internal:8094
docker compose up -d --build
curl -s http://127.0.0.1:8082/health
```

### 暴露公网（Cloudflare Tunnel 示例）

在 heyi-bj 的 `cloudflared` 配置加一条：

```yaml
ingress:
  - hostname: echodesk.yoliyoli.uk
    service: http://localhost:8082
  # ... 其余既有条目
```

`cloudflared tunnel route dns <tunnel> echodesk.yoliyoli.uk` 后客户端即可用 `https://echodesk.yoliyoli.uk` 访问。

当前已完成公网 smoke test：

- `GET /health` → `200`
- 无 token 调 `POST /v1/chat/completions` → `401`
- 带 token 调 `MiniMax-M2.7` chat → `200`
- 带 token 调 `Qwen3-1.7B` fast chat → `200`
- 带 token 调 `POST /v1/audio/speech` → `200`
- 带 token 调 `POST /v1/audio/transcriptions` → `200`

## 安全清单

- [ ] `.env` 不入库（已在 `.gitignore`）；真实 key 只在服务端
- [ ] client token 用 `openssl rand` 生成，逐用户分发，可单独吊销（从白名单删除）
- [ ] 公网入口走 HTTPS（Cloudflare 隧道自带）
- [ ] 限流阈值按额度调整，防止单 token 刷爆 yunwu
- [ ] 如需多实例，将 RateLimiter 换成 Redis 实现

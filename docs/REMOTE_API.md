# 远程后端：稳定对外 HTTPS Endpoint（Phase 4 · M_remote_api）

> **What**：把 heyi-bj 上的 3 个 GPU/AI 服务通过现有 cloudflared tunnel 暴露成稳定 HTTPS endpoint，让用户**不再依赖 Mac 本地 Tailscale daemon**。
>
> **Why**：2026-05-28 凌晨 Mac Tailscale 异常停了 8 小时，导致 4495 个 STT 请求全失败（用户没察觉，因为 utun 网卡假回环让 ICMP/TCP 看起来都通）。Tailscale 这种端到端 mesh 在客户端 daemon 异常时**故障静默**——切换到公网 HTTPS 让链路有明确 DNS/TLS 错误码。

## 1. Endpoint 一览

> **2026-05-28 起默认翻转**：`.env.example` 默认值改为公网 HTTPS endpoint，Tailscale IP 改为可选注释。原因见 §1.1。

| 服务 | 公网 HTTPS（**默认**） | Tailscale（可选备选） | 背后实现 |
|---|---|---|---|
| FireRedASR2 STT | **`https://stt.yoliyoli.uk`** | `http://100.87.251.9:8090` | heyi-bj :8090 (FireRedASR2-AED) |
| Qwen3-1.7B fast LLM | **`https://llm-fast.yoliyoli.uk/v1`** | `http://100.87.251.9:7860/v1` | heyi-bj :7860 (sglang vLLM) |
| Qwen3-TTS | **`https://tts.yoliyoli.uk`** | `http://100.87.251.9:8094` | heyi-bj :8094 |
| Echo backend (老 echo 项目) | `https://echo.yoliyoli.uk` | — | heyi-bj :8765（**与本文无关，不要混用**）|

## 1.1 为什么默认走公网 HTTPS（而不是 Tailscale）

**触发事件**：2026-05-28 凌晨 Mac Tailscale daemon 异常停止 8 小时，期间 4495 个 STT 请求全部失败，但用户毫无察觉——`utun` 网卡假回环让 ICMP/TCP 探测仍然"看似可达"，错误堆积在客户端日志里没有任何前端报警。

**结构性问题**：
- **Tailscale 是 mesh + 客户端 daemon 模式**：daemon 异常时**故障静默**——表象是连接超时/重置，没有明确"这是 Tailscale 挂了"的信号
- **公网 HTTPS 是显式 DNS + TLS 链路**：链路任一环节出问题都会抛出明确的错误码（`SSLError` / `ConnectionRefused` / `502/503/504`），日志可观测、监控可触发、用户可察觉

**翻转决策（2026-05-28，PR 本次）**：
- `.env.example` 默认值 = 公网 HTTPS（容错优先于延迟）
- Tailscale IP 保留为注释，BJ wifi 局域网内追求最低延迟时手工切换
- 延迟 trade-off：公网 ~300-1000ms vs Tailscale ~30ms。日常对话/会议级延迟用户感知阈值约 500ms，可接受；STT/LLM 一次调用本身就是秒级，公网附加延迟占比 < 30%

**结论**：可观测性 > 极致延迟。默认走公网，Tailscale 当显式优化档位。

**链路**：

```
Mac (echo-demo backend)
   │
   ├── Tailscale 路径：直连 100.87.251.9:<port>（局域网最低延迟，但依赖 mac 端 Tailscale daemon）
   │
   └── 公网路径：HTTPS → Cloudflare 边缘 → cloudflared tunnel(heyi-bj) → localhost:<port>
        ↑ 失败时直接抛 DNS / TLS / HTTP 错误码，故障可见
```

cloudflared tunnel 是 outbound persistent connection（heyi-bj 主动连到 Cloudflare），所以**heyi-bj 不需要公网入站端口、不需要静态公网 IP**。

## 2. 切换方法（用户视角）

### 默认状态：已经走公网 HTTPS，无需任何改动

`cp .env.example .env` 后，STT / LLM_FAST / TTS 三个 base url **默认就是公网 HTTPS**。`uvicorn` 启动后即生效，无需手动切换。

如果想切到 Tailscale（仅在 BJ wifi 局域网内 + 追求 < 50ms 延迟时推荐，且确认 Mac Tailscale daemon 健康），见下文方式 A / B / C 之一。

### 方式 A：回切到 Tailscale（持久，改 `.env`）

在 `~/Desktop/all/echo-demo/.env` 里找到 STT / LLM_FAST / TTS 三段，把**默认 HTTPS 注释掉、把备选 Tailscale 行解开注释**：

```bash
# 公网 HTTPS endpoint（默认）
# STT_FIRERED_URL=https://stt.yoliyoli.uk
# 备选：Tailscale 内网
STT_FIRERED_URL=http://100.87.251.9:8090

# LLM_FAST_BASE_URL=https://llm-fast.yoliyoli.uk/v1
LLM_FAST_BASE_URL=http://100.87.251.9:7860/v1

# TTS_QWEN3_URL=https://tts.yoliyoli.uk
TTS_QWEN3_URL=http://100.87.251.9:8094
```

然后重启 backend（`pkill -f 'uvicorn.*echodesk'` 或 EchoDesk 桌面端 Settings → Restart Backend）。

**回切前自检（2 步）**：
1. `tailscale status` 看 `100.87.251.9` 是否 online（不是 `expired` / `offline`）
2. `nc -zv 100.87.251.9 8090` 在 1 秒内返回 `succeeded`

任一不过 → **不要切回 Tailscale**，留在默认公网。

### 方式 B：改 `~/.echodesk/config.json`（运行时覆盖）

EchoDesk 桌面端 Settings Panel（Phase 3 已上线，PR #44）支持远端配置：

```json
{
  "stt_firered_url": "http://100.87.251.9:8090",
  "llm_fast_base_url": "http://100.87.251.9:7860/v1",
  "tts_qwen3_url": "http://100.87.251.9:8094"
}
```

EchoDesk 启动时会优先读这个文件并 override `.env`（详见 `backend/app/config.py` 加载顺序）。删除该文件即恢复默认（公网 HTTPS）。

### 方式 C：单条 endpoint 临时切（debug）

```bash
STT_FIRERED_URL=http://100.87.251.9:8090 \
  uvicorn echodesk.main:app --reload --port 8769
```

只对当前进程生效，不动 `.env`。

## 3. 公网 HTTPS vs Tailscale：什么时候选哪个

> **新默认（2026-05-28）**：默认公网 HTTPS。下表只在「主动想要更低延迟且能保证 Tailscale 健康」时才切回 Tailscale。

| 场景 | 推荐 | 理由 |
|---|---|---|
| **任何场景的安全默认** | 公网 HTTPS | 故障可观测、错误可见、没有客户端 daemon 隐式依赖 |
| **BJ wifi 局域网 + Tailscale 已确认健康 + 追求最低延迟** | Tailscale | 走 LAN，RTT ~30ms，省掉 CF 边缘 |
| **出差/外地 + Tailscale 当 derp 走 hkg** | 公网 HTTPS | Tailscale relay 增加 ~300ms，CF 全球 anycast 边缘通常更近 |
| **Tailscale daemon 不稳/重启中/状态可疑** | 公网 HTTPS | 不依赖本地 daemon，故障时有明确 DNS/TLS/HTTP 错误码 |
| **CI / 任何容器 / 临时机器** | 公网 HTTPS | 不用装 Tailscale + 登录 |

**延迟参考**（2026-05-28 测，单次 curl 包含 TCP + TLS 握手）：

| 路径 | 测点 | RTT (root path → 404) |
|---|---|---|
| heyi-bj 自身回环 → stt.yoliyoli.uk（CF 全球） | heyi-bj 公网出 | ~0.85s 首次 / ~0.10s 复用连接 |
| Mac BJ wifi → 100.87.251.9 (Tailscale LAN) | 本地 | ~0.02s（直连）|
| Mac BJ wifi → stt.yoliyoli.uk (Tailscale DNS 异常时) | 本地 | DNS / TLS 失败，明确报错 |

**结论**：默认公网 HTTPS（容错优先）；只在确认 Tailscale 健康且想要 < 50ms 延迟时手工切回。

## 4. 故障切换 SOP

> 因为新默认已经是公网 HTTPS，「Tailscale 异常」不再需要切换 SOP——默认状态下不依赖 Tailscale。这里保留两个仍可能发生的故障场景的 SOP。

### 4.1 公网 endpoint 异常（heyi-bj 端 cloudflared / 公网链路故障）

**触发条件**：任一症状
- 桌面端连续 ≥ 3 次 STT/LLM/TTS 请求收到 `502 / 503 / 504` 或 DNS 解析失败
- `~/.echodesk/logs/backend.log` 出现大量 `SSLError` / `ConnectionError` 指向 `*.yoliyoli.uk`
- `curl -m 10 https://stt.yoliyoli.uk/docs` 直接超时

**切换步骤（回到 Tailscale 当备用，约 5 分钟）**：

1. **先确认 Tailscale 链路本身健康**：
   ```bash
   tailscale status                       # 看 100.87.251.9 是否 online
   nc -zv 100.87.251.9 8090               # 1 秒内 succeeded 才 OK
   curl -m 5 http://100.87.251.9:8090/docs | head -c 100
   ```

   如果 Tailscale 也异常 → 公网和 Tailscale 都断了，去查 heyi-bj（机器宕机 / 断电 / 断网）。

2. **改 `.env`**（按上文 §2 方式 A，把公网注释、Tailscale 解开注释）。

3. **重启 backend**：
   ```bash
   pkill -f 'uvicorn.*echodesk'   # 或 EchoDesk 桌面端 Settings → Restart Backend
   ```

4. **验证桌面端 4 类调用恢复**：触发一次会议录音 → 看 STT 落字、LLM 路由、TTS 播报。

5. **cloudflared 修好后切回默认**：把 `.env` 中两段注释翻回去（公网解开、Tailscale 注释），重启 backend。或者直接 `cp .env.example .env` 重置默认值（注意先备份个人密钥）。

### 4.2 Tailscale 异常（已是默认状态，无需切换）

新默认下，`.env` 默认值已经是公网 HTTPS，**Tailscale daemon 异常不会影响 backend**。
仅当**手动切到 Tailscale 后**遇到 daemon 异常，才需要按上文 §2 方式 A 切回公网（即把 `.env` 还原为 `.env.example` 默认值）。

## 5. heyi-bj 端配置（实施记录，2026-05-28）

### 涉及组件
- `cloudflared` v2026.5.0
- tunnel id: `bfd33448-3605-47c5-813d-70924ae5cd09`（name=`echo-heyi`，已存在）
- config: `/home/ai/.cloudflared/config.yml`
- 备份: `/home/ai/.cloudflared/config.yml.bak-1779931059`

### 新 ingress 配置（已部署）

```yaml
tunnel: bfd33448-3605-47c5-813d-70924ae5cd09
credentials-file: /home/ai/.cloudflared/bfd33448-3605-47c5-813d-70924ae5cd09.json

originRequest:
  connectTimeout: 10s
  tlsTimeout: 10s
  tcpKeepAlive: 30s
  keepAliveConnections: 10
  keepAliveTimeout: 90s
  noTLSVerify: true

ingress:
  - hostname: echo.yoliyoli.uk          # 老 echo 项目，不动
    service: http://localhost:8765
  - hostname: stt.yoliyoli.uk
    service: http://localhost:8090
  - hostname: llm-fast.yoliyoli.uk
    service: http://localhost:7860
  - hostname: tts.yoliyoli.uk
    service: http://localhost:8094
  - service: http_status:404
```

### DNS（已添加 3 条 Cloudflare proxied CNAME）

```
stt.yoliyoli.uk       CNAME  bfd33448-3605-47c5-813d-70924ae5cd09.cfargotunnel.com  (proxied)
llm-fast.yoliyoli.uk  CNAME  bfd33448-3605-47c5-813d-70924ae5cd09.cfargotunnel.com  (proxied)
tts.yoliyoli.uk       CNAME  bfd33448-3605-47c5-813d-70924ae5cd09.cfargotunnel.com  (proxied)
```

通过本机 `cloudflared tunnel route dns echo-heyi <hostname>` 命令添加（origin cert 在 Mac 本机）。

### 启动方式（**已知 follow-up**）

当前 named tunnel 由手工启动（`/usr/local/bin/cloudflared tunnel --config /home/ai/.cloudflared/config.yml run`），**没有 systemd 守护**。机器重启后这 4 个对外 endpoint 都会失效。

`echo-tunnel.service`（systemd user unit）目前跑的是 quick tunnel（`--url http://localhost:8765`），输出 `*.trycloudflare.com` 临时域名，**没有被任何稳定域名指向，实际无用**。

**TODO（后续 PR）**：把 `echo-tunnel.service` 的 `ExecStart` 改成
```
ExecStart=/usr/local/bin/cloudflared tunnel --config /home/ai/.cloudflared/config.yml run
```
让 systemd 接管 named tunnel。会有 ~10 秒切换中断，所以单独 PR 处理。

## 6. 安全提示

| 项 | 状态 | 备注 |
|---|---|---|
| TLS | Cloudflare 自动 LetsEncrypt | 用户侧 HTTPS，cloudflared → 本地服务是 HTTP（noTLSVerify=true）|
| 鉴权（老 3 个域名 stt/llm-fast/tts） | **optional**：带正确 Bearer 校验，不带也放过（向后兼容 echo-demo）| 2026-05-28 加了 `heyi-gateway` nginx，详见 §8 |
| 鉴权（新 2 个域名 llm/tts2） | **strict Bearer**：必须带 `Authorization: Bearer <token>` | 2026-05-28 新增，详见 §8 |
| 速率限制 | Cloudflare 默认 + cloudflared `keepAliveConnections=10` | 单 IP / 单分钟级 |
| 日志 | cloudflared 写 `/home/ai/cloudflared.log` + `docker logs heyi-gateway` | 后者包含 nginx access log |

**残留风险**：老 3 个域名（stt/llm-fast/tts）当前是 _optional auth_——不带 token 也通，目的是不破坏 echo-demo 现网调用。**这是过渡态**，目标终态是切到 strict Bearer（见 §8 路线图）。

**当前可接受**因为：(a) 域名没公开，(b) 用户单人使用，(c) 流量 < 100 req/day，Cloudflare 全局 free tier 没问题。

## 7. 验证

部署后（2026-05-28）从 heyi-bj 自己 curl 公网 endpoint 全通：

```
echo.yoliyoli.uk/          http=404 time=0.85s  remote_ip=104.21.61.190  ✅ 回归
stt.yoliyoli.uk/docs       Swagger UI HTML       ✅ FireRedASR2
llm-fast.yoliyoli.uk/v1/models  {"id":"Qwen3-1.7B"}  ✅ sglang vLLM
tts.yoliyoli.uk/           http=404 (root 无路由)  ✅ Qwen3-TTS
```

> **本机 Mac 没法直接 curl 验证**：Tailscale magic DNS 把 `*.yoliyoli.uk` 劫持到 `198.18.0.X`（Tailscale 4via6），导致 SSL connect 失败。这是 Tailscale 客户端行为，**不代表公网 endpoint 异常**。
> 真实验证方式：
> - 从 heyi-bj 自己 curl（如上）
> - 从手机 4G/5G 网络（脱离 Tailscale）
> - 临时关 Tailscale magic DNS：`sudo tailscale set --accept-dns=false` （测完记得恢复）

---

## 8. 公网 Auth 网关（2026-05-28 新增）

> **What**：在 cloudflared 与本地服务之间插入一个 nginx 容器 `heyi-gateway`（监听 `127.0.0.1:8081`），按 `Host` 头分流到各 GPU 服务并校验 `Authorization: Bearer <token>`。
>
> **Why**：原 4 个公网 endpoint 是裸跑无 auth（见 §6 历史版本），任何知道域名的人都可白嫖 GPU；同时新增 `llm.yoliyoli.uk` 暴露 Qwen3-32B-AWQ + `tts2.yoliyoli.uk` 暴露 CosyVoice2，对公网开放就必须有 auth。

### 8.1 当前公网域名一览（2026-05-28）

| 域名 | 后端 | 模型 | Auth 模式 | OpenAI 兼容 |
|---|---|---|---|---|
| `stt.yoliyoli.uk` | `:8090` firered | FireRedASR2-AED | optional Bearer | `/v1/audio/transcriptions` |
| `llm-fast.yoliyoli.uk` | `:7860` sglang | Qwen3-1.7B | optional Bearer | `/v1/{models,chat/completions,completions}` |
| `tts.yoliyoli.uk` | `:8094` fasterqwen3-tts | Qwen3-TTS (CustomVoice) | optional Bearer | `/v1/audio/speech` + `/v1/voices` |
| **`llm.yoliyoli.uk`** _(新)_ | `:7862` sglang | **Qwen3-32B-AWQ** | **strict Bearer** | `/v1/{models,chat/completions,completions}` |
| **`tts2.yoliyoli.uk`** _(新)_ | `:8092` cosyvoice2 | **CosyVoice2** | **strict Bearer** | `/v1/audio/speech` |

> "optional Bearer" = 不带 token 放过；带 token 必须正确（否则 401）。"strict Bearer" = 必须带正确 token。

### 8.2 Token 获取

```bash
cat ~/.echodesk/heyi_gateway.token   # 已在 Mac 本机生成（chmod 600）
```

> Token 来源：`heyi:openssl rand -hex 32`，固定形态 64 字符 hex 字符串。**轮转**时改 nginx.conf 的 map 值 + 同步更新 `~/.echodesk/heyi_gateway.token` 即可。

### 8.3 客户端调用示例（OpenAI Python SDK）

```python
from openai import OpenAI
from pathlib import Path

TOKEN = Path("~/.echodesk/heyi_gateway.token").expanduser().read_text().strip()

# Qwen3-32B-AWQ
client = OpenAI(base_url="https://llm.yoliyoli.uk/v1", api_key=TOKEN)
print(client.chat.completions.create(
    model="Qwen3-32B-AWQ",
    messages=[{"role": "user", "content": "hi"}],
    max_tokens=64,
).choices[0].message.content)

# Qwen3-1.7B fast（老域名也支持带 token）
fast = OpenAI(base_url="https://llm-fast.yoliyoli.uk/v1", api_key=TOKEN)
```

### 8.4 curl 验证（从 Mac，避开 Tailscale magic DNS 拦截）

```bash
TOKEN=$(cat ~/.echodesk/heyi_gateway.token)

# 1. 新增 32B（strict auth）
curl -H "Authorization: Bearer $TOKEN" https://llm.yoliyoli.uk/v1/models
curl -H "Authorization: Bearer $TOKEN" -X POST https://llm.yoliyoli.uk/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3-32B-AWQ","messages":[{"role":"user","content":"用一句话自我介绍"}],"max_tokens":80}'

# 2. 老域名（optional auth，带不带 token 都通）
curl https://llm-fast.yoliyoli.uk/v1/models
curl -H "Authorization: Bearer $TOKEN" https://llm-fast.yoliyoli.uk/v1/models   # 等价

# 3. 错误 token 应该被拒
curl -H "Authorization: Bearer wrong" https://llm-fast.yoliyoli.uk/v1/models    # → 401

# 4. 新 CosyVoice2
curl -H "Authorization: Bearer $TOKEN" -X POST https://tts2.yoliyoli.uk/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"cosyvoice2","input":"你好","voice":"default"}' --output /tmp/cosy.wav
```

> Tailscale magic DNS 会把 `*.yoliyoli.uk` 劫持到 `198.18.0.X`（4via6）导致 SSL connect 失败。临时绕过：`sudo tailscale set --accept-dns=false`（测完记得恢复）。

### 8.5 heyi-bj 端实施记录

```bash
# 1. nginx auth 网关（heyi-gateway 容器）
ssh heyi 'docker ps --filter name=heyi-gateway --format "{{.Names}}\t{{.Status}}"'
# 配置：/home/ai/heyi-gateway/nginx.conf（host network，监听 127.0.0.1:8081）
# 启动：docker run -d --name heyi-gateway --network host --restart always \
#         -v /home/ai/heyi-gateway/nginx.conf:/etc/nginx/nginx.conf:ro nginx:alpine

# 2. cloudflared config 路由更新（旧 4 条 + 新 2 条 → 全部指向 :8081 网关）
ssh heyi 'cat ~/.cloudflared/config.yml'
# 备份：/home/ai/.cloudflared/config.yml.bak-1779931059  /home/ai/.cloudflared/config.yml.bak-<ts>

# 3. cloudflared 进程：systemd --user 守护（PID 由 PPID 2386 = systemd --user 拉起）
ssh heyi 'ps aux | grep cloudflared | grep -v grep'
```

cloudflared **保留旧 `echo.yoliyoli.uk → :8765` 不动**（echo backend 直通，无 auth；与本项目无关，按原状）。其余 5 个全部指向 `localhost:8081`（nginx gateway）。

### 8.6 已知 TODO（ambiguity 兜底）

| # | 项 | 当前状态 | 卡在哪 | 用户需做 |
|---|---|---|---|---|
| 1 | **DNS for `llm.yoliyoli.uk` / `tts2.yoliyoli.uk`** | nginx + cloudflared ingress 已就绪，DNS 未解析 | heyi 上无 `cert.pem`，`cloudflared tunnel route dns` 跑不了 | 在 Cloudflare Dashboard `yoliyoli.uk` 区里手工加两条 CNAME（见下） |
| 2 | **老 3 域名升级为 strict Bearer** | 仍是 optional auth（兼容期）| 切之前要先确保所有客户端都带 token | echo-demo 一侧加 `Authorization` header 注入后再切 |
| 3 | **cloudflared 切 systemd 守护 named tunnel** | 当前 user-systemd 已守护 | 跑得起来，但 unit 文件还没归档到仓库 | （已在原 §5 TODO，本次不动） |

**DNS 配置**（用户在 Cloudflare Dashboard 加一次性）：

```
llm.yoliyoli.uk    CNAME  bfd33448-3605-47c5-813d-70924ae5cd09.cfargotunnel.com   (proxied)
tts2.yoliyoli.uk   CNAME  bfd33448-3605-47c5-813d-70924ae5cd09.cfargotunnel.com   (proxied)
```

加完 DNS 后立即可用（cloudflared ingress 已经准备好了）。

### 8.7 回滚预案

如果 nginx 网关挂了/认证逻辑出错导致 echo-demo 链路异常：

```bash
# 立即恢复直通（绕过 nginx，直接 cloudflared → 后端）
ssh heyi 'cp ~/.cloudflared/config.yml.bak-1779931059 ~/.cloudflared/config.yml \
  && pkill -HUP cloudflared'   # systemd --user 自动拉起新进程
# 副作用：丢失 32B + CosyVoice2 域名，4 个老域名回到无 auth 裸跑
```

# 远程后端：稳定对外 HTTPS Endpoint（Phase 4 · M_remote_api）

> **What**：把 heyi-bj 上的 3 个 GPU/AI 服务通过现有 cloudflared tunnel 暴露成稳定 HTTPS endpoint，让用户**不再依赖 Mac 本地 Tailscale daemon**。
>
> **Why**：2026-05-28 凌晨 Mac Tailscale 异常停了 8 小时，导致 4495 个 STT 请求全失败（用户没察觉，因为 utun 网卡假回环让 ICMP/TCP 看起来都通）。Tailscale 这种端到端 mesh 在客户端 daemon 异常时**故障静默**——切换到公网 HTTPS 让链路有明确 DNS/TLS 错误码。

## 1. Endpoint 一览

| 服务 | Tailscale（默认）| 公网 HTTPS（可选） | 背后实现 |
|---|---|---|---|
| FireRedASR2 STT | `http://100.87.251.9:8090` | **`https://stt.yoliyoli.uk`** | heyi-bj :8090 (FireRedASR2-AED) |
| Qwen3-1.7B fast LLM | `http://100.87.251.9:7860/v1` | **`https://llm-fast.yoliyoli.uk/v1`** | heyi-bj :7860 (sglang vLLM) |
| Qwen3-TTS | `http://100.87.251.9:8094` | **`https://tts.yoliyoli.uk`** | heyi-bj :8094 |
| Echo backend (老 echo 项目) | — | `https://echo.yoliyoli.uk` | heyi-bj :8765（**与本文无关，不要混用**）|

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

### 方式 A：改 `.env`（持久生效）

在 `~/Desktop/all/echo-demo/.env` 里找到 STT/LLM_FAST/TTS 三段，**任选**需要走公网的服务替换：

```bash
# 注释掉原 Tailscale IP
# STT_FIRERED_URL=http://100.87.251.9:8090
STT_FIRERED_URL=https://stt.yoliyoli.uk

# LLM_FAST_BASE_URL=http://100.87.251.9:7860/v1
LLM_FAST_BASE_URL=https://llm-fast.yoliyoli.uk/v1

# TTS_QWEN3_URL=http://100.87.251.9:8094
TTS_QWEN3_URL=https://tts.yoliyoli.uk
```

然后重启 backend（`pkill -f 'uvicorn.*echodesk'` 或 EchoDesk 桌面端 Settings → Restart Backend）。

### 方式 B：改 `~/.echodesk/config.json`（运行时覆盖）

EchoDesk 桌面端 Settings Panel（Phase 3 已上线，PR #44）支持远端配置：

```json
{
  "stt_firered_url": "https://stt.yoliyoli.uk",
  "llm_fast_base_url": "https://llm-fast.yoliyoli.uk/v1",
  "tts_qwen3_url": "https://tts.yoliyoli.uk"
}
```

EchoDesk 启动时会优先读这个文件并 override `.env`（详见 `backend/app/config.py` 加载顺序）。

### 方式 C：单条 endpoint 临时切（debug）

```bash
STT_FIRERED_URL=https://stt.yoliyoli.uk \
  uvicorn echodesk.main:app --reload --port 8769
```

只对当前进程生效，不动 `.env`。

## 3. Tailscale vs 公网：什么时候选哪个

| 场景 | 推荐 | 理由 |
|---|---|---|
| **在 BJ wifi 局域网内 + heyi-bj 也在 BJ** | Tailscale | 走 LAN，RTT < 1ms，没有 CF 入口环节 |
| **出差/外地 + Tailscale 当 derp 走 hkg** | 公网 HTTPS | Tailscale relay 增加 ~300ms，CF 全球 anycast 边缘通常更近 |
| **Tailscale daemon 不稳/重启中** | 公网 HTTPS | 不依赖本地 daemon，能立刻定位故障 |
| **CI / 任何容器 / 临时机器** | 公网 HTTPS | 不用装 Tailscale + 登录 |

**延迟参考**（2026-05-28 测，单次 curl 包含 TCP + TLS 握手）：

| 路径 | 测点 | RTT (root path → 404) |
|---|---|---|
| heyi-bj 自身回环 → stt.yoliyoli.uk（CF 全球） | heyi-bj 公网出 | ~0.85s 首次 / ~0.10s 复用连接 |
| Mac BJ wifi → 100.87.251.9 (Tailscale LAN) | 本地 | ~0.02s（直连）|
| Mac BJ wifi → stt.yoliyoli.uk (Tailscale DNS 异常时) | 本地 | DNS / TLS 失败，明确报错 |

**结论**：日常 BJ wifi 用 Tailscale，外地 / Tailscale 异常时切公网。

## 4. 故障切换 SOP

### 触发条件
任一以下症状：
- 桌面端连续 ≥ 3 次 STT/LLM/TTS 请求超时
- `~/.echodesk/logs/backend.log` 出现大量 `requests.exceptions.ConnectionError` 指向 `100.87.251.9`
- `tailscale status` 报 `state=NoState` 或 `Stopped`
- `nc -zv 100.87.251.9 8090` 30 秒不响应

### 步骤（5 分钟切换）

1. **验证公网链路是否健康**（**从手机热点或外网**，因为 Mac Tailscale 异常时本机 DNS 可能也被劫持）：
   ```bash
   curl -m 10 https://stt.yoliyoli.uk/docs | head -c 100
   curl -m 10 https://llm-fast.yoliyoli.uk/v1/models
   curl -m 10 https://tts.yoliyoli.uk/  # 返回 404 root path 即正常
   ```

2. **改 `.env`**（替换为公网 URL，见上文方式 A）。

3. **重启 backend**：
   ```bash
   # EchoDesk 桌面端 Settings → Restart Backend
   # 或命令行
   pkill -f 'uvicorn.*echodesk'
   ```

4. **验证桌面端 4 类调用恢复**：触发一次会议录音 → 看 STT 落字、LLM 路由、TTS 播报。

5. **修复 Tailscale 后切回（可选）**：把 `.env` 改回 Tailscale IP，再重启 backend。

### 回退到 Tailscale（heyi-bj 端 cloudflared 故障时）

如果 cloudflared 那边挂了（curl `stt.yoliyoli.uk` 超时），但 Tailscale 正常：

```bash
# 在 echo-demo/.env 把所有 https://... 那 3 行注释回去，恢复 http://100.87.251.9:...
```

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
| 鉴权 | **无** | 4 个 endpoint 当前对全公网开放，任何人知道域名即可调用 |
| 速率限制 | Cloudflare 默认 + cloudflared `keepAliveConnections=10` | 单 IP / 单分钟级 |
| 日志 | cloudflared 写 `/home/ai/cloudflared.log` | 包含访问路径，但没记 body |

**风险**：3 个 GPU 服务（FireRedASR2 / sglang / Qwen3-TTS）**现在是裸跑、无 token**，公网可调用 = GPU 算力可被白嫖。

**Mitigation（建议下个 PR 加）**：
- 在 `originRequest` 加 `httpHostHeader` + 在各 GPU 服务前加一个 sidecar nginx 校验 `X-Echo-Token`
- 或者用 Cloudflare Access (free tier) 加 Google OAuth 限制 google identity

当前接受这个风险因为：(a) 域名没公开，(b) 用户单人使用，(c) 流量 < 100 req/day，Cloudflare 全局 free tier 没问题。

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

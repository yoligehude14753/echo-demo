# heyi-bj · cloudflared named tunnel · systemd-user 守护 SOP

> Phase 4 · M_heyi_systemd · 2026-05-28
> 目标：heyi-bj 重启后 4 个 `*.example.com` 对外 HTTPS endpoint 自动恢复，无需人工 `nohup`。

---

## 1. 现状总览（必读）

### 1.1 域名 → 后端服务

| 公网域名 | 后端 (heyi 内网) | 用途 |
|---|---|---|
| `https://echo.example.com` | `http://localhost:8765` | echo backend |
| `https://stt.example.com` | `http://localhost:8090` | FireRedASR2 |
| `https://llm-fast.example.com` | `http://localhost:7860` | sglang vLLM Qwen3-1.7B |
| `https://tts.example.com` | `http://localhost:8094` | Qwen3-TTS |

全部走同一个 cloudflared named tunnel `your-tunnel-id`，路由表见 `/home/ai/.cloudflared/config.yml`。

### 1.2 systemd-user 单元命名约定（避免歧义）

| 单元名 | 职责 | 当前状态 |
|---|---|---|
| `cloudflared-echo.service` | **真正承担** 4 个 `*.example.com` 的 named tunnel 守护进程 | ✅ active running，已 enabled |
| `echo-tunnel.service.bak` | 历史遗留的 quick tunnel (`--url http://localhost:8765` → `*.trycloudflare.com`)，**无任何业务作用**。改名 `.bak` 防止后人误启动 | 已 disable + 改名归档 |

> ⚠️ **PR #50 (M_remote_api) body 中说"cloudflared 是手工 nohup 的孤儿进程 PID 1910186"是误判**：诊断时 `ps -ef` 显示 PPID=2386（即 user systemd PID 1 of user@1000），实际就是 `cloudflared-echo.service` 在守护。本 PR 在此基础上做了清理 + 演练验证。

### 1.3 enable-linger 状态

```
$ loginctl show-user ai -p Linger
Linger=yes
```

`Linger=yes` 是 systemd-user 在用户**未登录时**也能跑 service 的硬前提。heyi-bj 已设置。

---

## 2. 部署 SOP（首次部署 / 灾后重建）

### 2.1 前置条件

- 已安装 `cloudflared` 到 `/usr/local/bin/cloudflared`（当前版本 `2026.5.0`）
- 已有 named tunnel `your-tunnel-id`，credentials JSON 在 `/home/ai/.cloudflared/your-tunnel-id.json`
- Cloudflare DNS 已配 CNAME：`echo / stt / llm-fast / tts.example.com` → `<tunnel-id>.cfargotunnel.com`

### 2.2 写 `~/.cloudflared/config.yml`

```yaml
tunnel: your-tunnel-id
credentials-file: /home/ai/.cloudflared/your-tunnel-id.json

originRequest:
  connectTimeout: 10s
  tlsTimeout: 10s
  tcpKeepAlive: 30s
  keepAliveConnections: 10
  keepAliveTimeout: 90s
  noTLSVerify: true

ingress:
  - hostname: echo.example.com
    service: http://localhost:8765
  - hostname: stt.example.com
    service: http://localhost:8090
  - hostname: llm-fast.example.com
    service: http://localhost:7860
  - hostname: tts.example.com
    service: http://localhost:8094
  - service: http_status:404
```

### 2.3 写 `~/.config/systemd/user/cloudflared-echo.service`

```ini
[Unit]
Description=Cloudflare Tunnel (echo.example.com → localhost:8765)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --config /home/ai/.cloudflared/config.yml run
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=default.target
```

> Description 历史原因写的是 `echo.example.com`，实际承担 4 个域名，路由表以 `config.yml` 的 ingress 为准。后续如需更名，必须先 `systemctl --user disable` 再删 symlink 改名再 enable，避免破坏开机自启。

### 2.4 一键启用 + 守护

```bash
# enable-linger（关键！没有这步，用户登出后 service 全停）
sudo loginctl enable-linger ai

# 验证
loginctl show-user ai -p Linger    # 必须输出 Linger=yes

# 启动 systemd-user 单元
systemctl --user daemon-reload
systemctl --user enable cloudflared-echo.service
systemctl --user start cloudflared-echo.service

# 5s 后检查
sleep 5
systemctl --user status cloudflared-echo.service --no-pager
```

期望输出：

```
Active: active (running) since ...
Main PID: <pid> (cloudflared)
...
INF Registered tunnel connection connIndex=0 ...
INF Registered tunnel connection connIndex=1 ...
INF Registered tunnel connection connIndex=2 ...
INF Registered tunnel connection connIndex=3 ...
```

4 个 cloudflare connection 全 registered 才算成功。

### 2.5 endpoint 健康检查

```bash
for url in \
  https://echo.example.com/health \
  https://stt.example.com/docs \
  https://llm-fast.example.com/v1/models \
  https://tts.example.com/; do
  printf "%-50s " "$url"
  curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 8 "$url"
done
```

期望：

| URL | 期望 HTTP | 说明 |
|---|---|---|
| `echo /health` | 200 | echo backend 健康检查 |
| `stt /docs` | 200 | FireRedASR2 FastAPI Swagger UI |
| `llm-fast /v1/models` | 200 | 返回含 `Qwen3-1.7B` 的 JSON |
| `tts /` | 404 | root 无路由属正常，调用 `/api/...` 才有响应 |

---

## 3. 故障排查 cheatsheet

### 3.1 service 当前状态

```bash
systemctl --user status cloudflared-echo.service --no-pager
systemctl --user show cloudflared-echo.service -p MainPID,ActiveState,SubState,Restart,RestartUSec
```

### 3.2 看日志

```bash
# 优先看 systemctl status 末尾的 5-10 行（最近一次启动日志总在那里）
systemctl --user status cloudflared-echo.service --no-pager | tail -20

# 完整日志（heyi 当前 user journal 未开 Storage=persistent，可能 "No journal files found"）
journalctl --user -u cloudflared-echo.service -n 100 --no-pager
journalctl --user -u cloudflared-echo.service --since "1 hour ago" --no-pager

# 若 user journal 没东西，从系统 journal 抓（需 sudo）
sudo journalctl _UID=1000 -u cloudflared-echo.service -n 100 --no-pager

# 启用持久 user journal（一次性配置）
sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald
```

### 3.3 看 cloudflared 自身视角的 tunnel 状态

```bash
# 注意：tunnel info 命令要求 ~/.cloudflared/cert.pem (origin cert)，
# 如果只有 tunnel-id.json 的 credentials 而没有 cert.pem，会报：
#   ERR Cannot determine default origin certificate path
# 这不影响 tunnel 运行（cert.pem 仅用于 management API 操作，run 不需要）
# 要查 tunnel 状态请直接看 systemd status 的最近 INF Registered tunnel connection 行
/usr/local/bin/cloudflared tunnel info your-tunnel-id || true

# 列出本机上所有 cloudflared 进程
ps -ef | grep cloudflared | grep -v grep
# 期望：1 行，PPID=user systemd（pid 通常为 user@1000.service 主进程）
```

### 3.4 endpoint 不通怎么办？三段排查

```bash
# 1) 本机 origin 是否在跑？
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/health    # echo
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/docs      # stt
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7860/v1/models # llm-fast
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8094/          # tts
# 任一非 200 → 对应后端服务挂了，不是 tunnel 的锅；
# 排查相应 backend systemd-user service：echo-backend.service / echo-qwen.service / echo-sensevoice.service / Qwen3-TTS

# 2) cloudflared 进程是否 alive？
systemctl --user is-active cloudflared-echo.service

# 3) cloudflare 边到 origin 链路是否 OK？
# 检查最近 5min 是否有 "ERR" / "WARN" / "connection closed" / "no tunnel host" 关键字
systemctl --user status cloudflared-echo.service --no-pager | tail -30 | grep -E "ERR|WARN|closed"
```

### 3.5 常见报错及含义

| 报错 | 含义 | 处理 |
|---|---|---|
| `failed to sufficiently increase receive buffer size` | UDP buffer 内核默认偏小（QUIC 性能告警） | 仅影响性能不影响功能；如需消除：`sudo sysctl -w net.core.rmem_max=7500000 net.core.wmem_max=7500000` |
| `ICMP proxy will use ...` | INFO 日志，cloudflared 自动配 ICMP 代理 | 忽略 |
| `Unit echo-tunnel.service could not be found` | 老的 quick tunnel service，已主动废弃改名为 `.bak` | 预期内，忽略 |

---

## 4. 回滚 SOP（systemd 出问题改回 nohup）

> 仅在 `cloudflared-echo.service` 反复异常无法在 30 分钟内排查清楚时使用。回滚后 24h 内必须把根因查清楚再切回 systemd-user。

```bash
# 1) 停掉 systemd 单元
systemctl --user stop cloudflared-echo.service
systemctl --user disable cloudflared-echo.service

# 2) 直接 nohup 拉起（临时）
nohup /usr/local/bin/cloudflared tunnel --config /home/ai/.cloudflared/config.yml run \
  > ~/cloudflared.log 2>&1 &
echo "pid=$!"   # 记录下来

# 3) 验证 4 endpoint
for url in echo stt llm-fast tts; do
  printf "%-50s " "https://$url.example.com/"
  curl -sS -o /dev/null -w "%{http_code}\n" --max-time 5 "https://$url.example.com/"
done

# 4) 24h 内务必切回 systemd（见 §2.4），切回前先 kill 这个 nohup pid
```

---

## 5. 重启演练（验证 Restart=always 真的 work）

`systemd-user` 守护是否真的能在崩溃后自动拉起，必须实测，不能信文档。**禁止真的 reboot heyi 生产机**，用 `SIGKILL` 模拟崩溃即可。

```bash
# before
systemctl --user show cloudflared-echo.service -p MainPID --value
# 例：1910186

# 模拟崩溃
systemctl --user kill -s SIGKILL cloudflared-echo.service

# 等待 ≥ RestartSec (=5s) + 启动时间，给 12s buffer
sleep 12

# after — PID 应该是新数字 + ActiveState=active
systemctl --user show cloudflared-echo.service -p MainPID,ActiveState --value
# 例：
#   1956981
#   active

# 再次 endpoint 健康检查
for url in https://stt.example.com/docs https://llm-fast.example.com/v1/models https://tts.example.com/; do
  printf "%-50s " "$url"
  curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" --max-time 8 "$url"
done
```

### 2026-05-28 实测记录（M_heyi_systemd PR）

```
[before] MainPID = 1910186
[kill -9] systemctl --user kill -s SIGKILL cloudflared-echo.service
[wait 12s]
[after]  MainPID = 1956981  (新 PID → Restart=always 生效)
[after]  ActiveState = active

drill 后 endpoint:
  https://stt.example.com/docs                       200 0.855981s
  https://llm-fast.example.com/v1/models             200 0.842579s
  https://tts.example.com/                           404 0.819379s   (root 无路由属正常)

journalctl tail (status 命令末尾):
  10:12:42 Started cloudflared
  10:12:42 INF Starting metrics server on 127.0.0.1:20241/metrics
  10:12:42 INF Registered tunnel connection connIndex=0
  10:12:43 INF Registered tunnel connection connIndex=1
  ...
```

实测从 SIGKILL 到 `Active: active (running)` 大约 5-7s（受 RestartSec=5 控制）。

---

## 6. 已知 follow-up（不在本 PR 范围）

1. **3 个 GPU endpoint 全裸跑无鉴权**：`stt / llm-fast / tts` 公网可访问，无任何 token / IP 白名单。短期靠"域名足够冷僻 + cloudflare WAF"勉强糊，正式上线前必须加 Cloudflare Access (Zero Trust) 或 Bearer Token 中间件。
2. **user journal 未持久化**：`journalctl --user -u cloudflared-echo.service` 当前显示 "No journal files were found"。一次性修复见 §3.2 末尾。当前依赖 `systemctl status` 末尾的 in-memory 日志和 cloudflared 自身的 stdout，重启后历史丢失。
3. **`cloudflared tunnel info` 不可用**：缺 `~/.cloudflared/cert.pem`（origin cert）。不影响 tunnel 运行，但无法用官方 CLI 查 connection 详情。如需启用：在有 cloudflare 账号的本地机器跑 `cloudflared tunnel login` 拿到 cert.pem 后 scp 上去。
4. **`cloudflared-echo.service` 命名误导**：名字像只管 echo backend，实际同一个 tunnel 承担 4 个域名。建议下次窗口期改名为 `cloudflared-tunnel.service` 或 `cloudflared-yoliyoli.service`。

---

## 7. 关键文件一览（heyi-bj 上）

| 路径 | 内容 |
|---|---|
| `/usr/local/bin/cloudflared` | 二进制（版本 `2026.5.0`） |
| `/home/ai/.cloudflared/config.yml` | tunnel 路由配置（4 个 ingress） |
| `/home/ai/.cloudflared/your-tunnel-id.json` | tunnel credentials（**含密钥，不要外泄**） |
| `/home/ai/.cloudflared/config.yml.bak-*` | config 历史快照 |
| `/home/ai/.config/systemd/user/cloudflared-echo.service` | **真正的** named tunnel 守护单元 |
| `/home/ai/.config/systemd/user/echo-tunnel.service.bak` | 历史 quick tunnel 单元（已废弃） |
| `/home/ai/.config/systemd/user/default.target.wants/cloudflared-echo.service` | enable 后的 symlink |
| `/tmp/cloudflared-echo.service.snapshot-*` | 本次变更前的实际快照备份 |

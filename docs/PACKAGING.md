# EchoDesk 打包与分发

把 EchoDesk 打包成 macOS dmg / Windows exe，并通过 **echo-gateway** 让外部用户「联网就能用」。

## 架构（方案 B：本地后端 + 云模型网关）

```
┌─────────────────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│ EchoDesk 客户端              │        │  echo-gateway     │        │  上游真实服务      │
│  Electron UI                │        │ (heyi-bj 部署)    │        │                  │
│  + 本地 Python orchestrator │  HTTPS │  Bearer token     │  真凭证 │  yunwu (主 LLM)   │
│   (会议/RAG/编排, 本机数据) ├───────▶│  鉴权 + 限流      ├───────▶│  heyi fast LLM    │
│  client token (无真实 key)  │        │  OpenAI 兼容反代  │        │  FireRed STT      │
└─────────────────────────────┘        └──────────────────┘        │  Qwen3 TTS        │
                                                                     └──────────────────┘
```

**为什么不把 yunwu key 加密塞客户端**：任何下发到客户端的密钥都能被逆向提取。唯一安全做法是 key 只存在网关服务端，客户端带自己的 client token，由网关回源。详见 `gateway/README.md`。

## 一、部署云端网关（前置，对外分发必做）

在 heyi-bj（已有 STT/TTS/fast-LLM + `*.yoliyoli.uk` 隧道）上：

```bash
cd echo-demo/gateway
cp .env.example .env          # 填真实 YUNWU_OPEN_KEY + 生成 client token
docker compose up -d --build
curl http://127.0.0.1:8082/health
```

当前公网入口已部署为 `https://echodesk.yoliyoli.uk`，Cloudflare 隧道映射到 heyi-bj 本机 `127.0.0.1:8082`。完整 SOP 见 `gateway/README.md`。

生成 client token（逐用户一个，可单独吊销）：

```bash
openssl rand -hex 24
```

## 二、客户端配置（网关模式）

在用户的 `~/.echodesk/config.json`（Windows：`%USERPROFILE%\.echodesk\config.json`）填：

```json
{
  "echo_gateway_url": "https://echodesk.yoliyoli.uk",
  "echo_gateway_token": "<分发给该用户的 client token>"
}
```

填了这两项即自动把主/快 LLM、STT、TTS 全部指向网关，客户端不再持有任何真实密钥（实现见 `backend/app/config.py` 的 `model_post_init`）。

## 三、打包桌面端（后端已内置，真·双击即用）

后端用 **PyInstaller 冻结成自带二进制**（`backend/packaging/echodesk-backend.spec`），由 electron-builder 的 `extraResources` 打进安装包。**用户无需再装 Python / venv**；Electron `main.cjs` 启动时优先用内置二进制（`resolveBundledBackend`，找不到才回退系统 Python）。

构建顺序（CI 已自动化；本地手动同理）：

```bash
# 1) 先冻结后端（在对应平台上跑：mac 出 mac 二进制，win 出 win 二进制）
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r packaging/requirements-build.txt
.venv/bin/pyinstaller --noconfirm packaging/echodesk-backend.spec   # → backend/dist/echodesk-backend/

# 2) 再打桌面安装包（extraResources 自动带上 dist/echodesk-backend）
cd ../desktop && npm ci
npm run app:dist        # macOS → release/*.dmg, *.zip（约 1GB，含 torch）
npm run app:dist:win    # Windows（须在 Windows 上）→ release/*.exe（NSIS + portable）
```

> `.exe` 必须在 Windows（或 CI windows runner）上构建，macOS 无法直接产 exe；二进制同理（PyInstaller 不跨平台交叉编译）。

### `install-backend.{sh,ps1}` 现在是可选

内置二进制后，普通用户不需要它。它仍用于：① dev（直接用源码+系统 venv）；② 写默认 `~/.echodesk/config.json`。用户用内置包时，只需在 `config.json` 填网关 url+token（见第二节）即可，无需任何 Python 步骤。

## 四、CI 自动出包（`.github/workflows/release.yml`）

打 tag 即在 mac + windows runner 上：**先 PyInstaller 冻结后端 → 再 electron-builder 打安装包**，产物传到 **draft Release**（私有仓）：

```bash
git tag v0.2.1 && git push origin v0.2.1
# Actions 跑完后在仓库 Releases 里看到 dmg + exe（draft），确认后发布
```

也可 `workflow_dispatch` 手动触发只验证构建、不发 release。

## 仍待完成 / 注意事项

1. **代码签名/公证**：mac dmg 未签名/未公证（首次打开需右键-打开）；Windows exe 未签名（SmartScreen 会警告）。要广泛分发需配证书（CI 里加 `CSC_LINK`/`CSC_KEY_PASSWORD` 等 secrets）。
2. **包体积**：含 torch（本地 ECAPA 声纹），mac .app ~1GB。若要瘦身，可把声纹也走网关，去掉客户端 torch（届时二进制可缩到 ~80MB）。
3. **泄露密钥轮换**：`.env.example` 历史里曾硬编码一枚真实 yunwu key（已改占位符），**务必在 yunwu 后台吊销并换新**。
4. **网关运维**：当前 heyi-bj 上 `echo-gateway` 容器监听宿主机 `8082`，公网通过 `echodesk.yoliyoli.uk` 进入；新增/吊销用户只需要维护服务端 `ECHO_GW_TOKENS` 白名单并重启容器。

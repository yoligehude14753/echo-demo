# EchoDesk 公开分发方案

## 目标

EchoDesk 采用“双仓分发”：

- 私有源码仓：`yoligehude14753/echo-demo`，保留完整源码、构建流程、网关实现和内部文档。
- 公开分发仓：`yoligehude14753/echodesk-public`，只放用户文档、闭源许可证、安全说明和 Release 安装包。

这不是开源模式，而是公开下载 + 闭源授权 + Access Key 使用模式。用户可以拿到 GitHub 链接下载安装，但不能看到完整源码，也拿不到真实服务密钥。

## 用户视角

用户只需要：

1. 打开 `https://github.com/yoligehude14753/echodesk-public`。
2. 从 Releases 下载 `.dmg` 或 `.exe`。
3. 安装 EchoDesk。
4. 在设置页填写：

```text
服务网关地址：https://echodesk.yoliyoli.uk
访问 Key：维护者分发给该用户的 key
```

## 公开仓允许包含

- `README.md`
- `LICENSE.md` / EULA
- `SECURITY.md`
- 用户安装说明
- Access Key 使用说明
- Release 安装包：`.dmg`、`.exe`、`.zip`
- `SHA256SUMS-*.txt`

## 公开仓禁止包含

- `backend/`
- `desktop/src/`
- `gateway/`
- `.env`、真实 key、token、Cloudflare 凭证
- heyi-bj 内部 IP、tunnel credentials
- source map：`*.map`
- PyInstaller spec 中暴露内部路径的调试产物
- 测试 fixture 中的敏感音频或日志

## 发布流程

### 1. 配置 GitHub Secret

在私有源码仓 `echo-demo` 配置：

```text
PUBLIC_RELEASE_TOKEN=<可写 yoligehude14753/echodesk-public 的 GitHub token>
```

Token 至少需要公开分发仓的 `contents:write` 权限，用于：

- 同步 `public/` 文档到公开仓 main 分支
- 在公开仓创建 Release
- 上传安装包和校验和

### 2. 更新公开仓文档

公开仓内容源在私有仓：

```text
public/
├── README.md
├── LICENSE.md
├── SECURITY.md
└── docs/
```

手动同步：

```bash
PUBLIC_REPO=yoligehude14753/echodesk-public scripts/sync-public-repo.sh
```

### 3. 打 tag 发布

```bash
git tag v0.2.1
git push origin v0.2.1
```

`.github/workflows/publish-public-release.yml` 会：

1. 在 macOS runner 冻结后端并构建 `.dmg/.zip`。
2. 在 Windows runner 冻结后端并构建 `.exe`。
3. 生成 `SHA256SUMS-*.txt`。
4. 同步 `public/` 文档到公开仓。
5. 把安装包上传到公开仓 Release。

## 当前已完成

- 公开仓已创建：`https://github.com/yoligehude14753/echodesk-public`
- 公开仓可见性：public
- 私有源码仓仍保持 private
- 公网网关已部署：`https://echodesk.yoliyoli.uk`
- 网关 `/health` 返回 200
- 无 Access Key 调用返回 401
- 带 Access Key 的 Yunwu chat / heyi-fast chat / TTS / STT 已通过公网 smoke test

## 发布前检查

每次公开 Release 前检查：

- [ ] 安装包里没有 `*.map`
- [ ] 安装包里没有 `.env`、`*.pem`、`*.key`
- [ ] 公开仓没有完整源码目录
- [ ] Release 附带 `SHA256SUMS-*.txt`
- [ ] `https://echodesk.yoliyoli.uk/health` 返回 200
- [ ] 使用有效 Access Key 跑一次 chat + TTS + STT smoke
- [ ] 使用无效 Access Key 确认返回 401


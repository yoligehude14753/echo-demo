# EchoDesk

EchoDesk 是一个桌面办公助手，用来处理会议记录、日常写作、资料整理和语音办公任务。

## Get Started

### 1. Install EchoDesk

从 [Releases](https://github.com/yoligehude14753/echodesk-public/releases) 下载适合你系统的安装包：

- macOS Apple Silicon：下载 `EchoDesk-*-mac-arm64.dmg`
- Windows：安装包稍后提供

安装完成后打开 EchoDesk。

### 2. Configure Access

进入「设置 → 远程服务」，填写：

```text
服务网关地址：https://echodesk.yoliyoli.uk
访问 Key：由维护者单独分发给你的 key
```

保存后即可使用联网能力，包括主 LLM、fast LLM、语音转文字、文字转语音和文档生成。

### 3. Start Working

你可以开始使用：

- 会议记录与整理
- 日常文档写作
- 资料问答与检索
- 语音转文字
- 文字转语音

## Access Key

EchoDesk 的联网能力需要有效 Access Key。维护者会给特定用户单独发放 Key。

请不要把 Key 发到公开 issue、截图、日志或文档里。没有 Access Key，或 Key 错误 / 被吊销时，联网接口会返回 `401`。

## 关于 `https://echodesk.yoliyoli.uk`

`https://echodesk.yoliyoli.uk` 是 API 网关，不是网页。

- 直接打开根路径 `/` 返回 `{"detail":"Not Found"}` 是正常现象。
- 健康检查地址是 `https://echodesk.yoliyoli.uk/health`。
- EchoDesk 客户端实际调用 `/v1/chat/completions`、`/v1/audio/speech`、`/v1/audio/transcriptions` 等接口。

## 安全说明

只从本仓库 Releases 下载安装包。不要运行第三方转载的安装包，尤其是陌生仓库中的压缩包或“破解版”。

每个 Release 会尽量附带 `SHA256SUMS.txt`。下载后可校验：

```bash
shasum -a 256 EchoDesk-*
```

macOS / Windows 可能因为未签名安装包显示安全警告。确认来源是本仓库 Releases 后再继续安装。后续版本会接入代码签名和公证。

## Documentation

- [Install Guide](docs/INSTALL.md)
- [Security Policy](SECURITY.md)


# EchoDesk Install Guide

## 普通用户

你只需要三步：

1. 从 [Releases](https://github.com/yoligehude14753/echodesk-public/releases) 下载你的系统对应安装包。
2. 安装并打开 EchoDesk。
3. 在「设置 → 远程服务」填入服务网关地址和 Access Key。

```text
服务网关地址：https://echodesk.yoliyoli.uk
访问 Key：维护者发给你的 key
```

## macOS

1. 下载 `EchoDesk-*-mac-arm64.dmg`。
2. 打开 `.dmg`。
3. 把 `EchoDesk.app` 拖入 `Applications`。
4. 首次打开如果提示“无法验证开发者”，右键 `EchoDesk.app`，选择「打开」。
5. 首次使用录音功能时，允许麦克风权限。

## Windows

Windows 安装包稍后提供。当前 Release 先提供 macOS Apple Silicon 版本。

## 常见问题

### 打开 `https://echodesk.yoliyoli.uk/` 为什么是 Not Found？

正常。它是 API 网关，不是网页。

健康检查地址是：

```text
https://echodesk.yoliyoli.uk/health
```

### 我没有 Access Key 能不能用？

可以打开本地界面和使用部分本地能力，但主 LLM、fast LLM、TTS、STT 等联网能力需要有效 Access Key。

### Key 错了会怎样？

联网接口会返回 `401`。请检查设置页中的 Access Key 是否完整，是否包含多余空格，或联系维护者确认是否已被吊销。

### 需要安装 Python 或 Node 吗？

不需要。正式安装包已经内置本地运行组件。Python、Node、Docker 只用于高级构建场景。


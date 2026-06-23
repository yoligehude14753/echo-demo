# EchoDesk 智能电视安装

当前 TV 版本面向 Android TV、Google TV、以及常见国产 Android / AOSP 智能电视。
这类电视通常有「我的应用」入口，允许安装 APK，或可以打开 ADB 网络调试。

## 下载

从 GitHub Release 下载：

- `EchoDesk-0.2.3-smart-tv.apk`：电视直接安装用 APK。
- `EchoDesk-0.2.3-smart-tv-oneclick.zip`：电脑一键安装包，含 APK、macOS 脚本、Windows PowerShell 脚本。
- `https://yoligehude14753.github.io/echo-demo/tv-install.html`：电视浏览器安装页，可用遥控器直接选择下载按钮。

## 方法 A：电视浏览器安装

1. 让电视连接网络。
2. 用电视浏览器打开 `https://yoligehude14753.github.io/echo-demo/tv-install.html`。
3. 选择「下载电视 APK」。
4. 下载完成后按系统提示允许安装未知来源应用。
5. 安装完成后，从电视「我的应用」打开 EchoDesk。

## 方法 B：电脑一键安装

适合会议室电视已经打开开发者模式 / ADB 调试的情况。

1. 让电脑和电视连接同一个局域网。
2. 在电视设置中打开开发者模式和网络调试 / ADB 调试。
3. 查到电视 IP。
4. 解压 `EchoDesk-0.2.3-smart-tv-oneclick.zip`。
5. macOS：

```bash
./install-tv-macos.sh 192.168.1.23
```

6. Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-tv-windows.ps1 -TvIp 192.168.1.23
```

如果电视弹出 RSA 调试授权，先选择允许，再重新运行脚本。

## 方法 C：U 盘安装

1. 把 `EchoDesk-0.2.3-smart-tv.apk` 拷到 U 盘。
2. 在电视文件管理器里打开 APK。
3. 按提示允许安装未知来源应用。

## 后端连接

TV APK 只是 EchoDesk 前端壳，不包含 Python backend，也不包含真实 API key。
电视端需要访问运行中的 EchoDesk backend。

会议室内测建议：

```bash
cd backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8769
```

然后在电视端 EchoDesk 设置里填电脑局域网地址，例如：

```text
http://192.168.1.20:8769
```

## 兼容边界

- 可安装：Android TV / Google TV / 国产 Android 或 AOSP 智能电视 / Android 电视盒子。
- 不可直接安装 APK：Samsung Tizen、LG webOS、Apple TV。
- 非 Android 电视要使用 EchoDesk，建议接一个 Android TV 盒子，或走后续 PWA / 浏览器版本。

## 安全说明

- APK 与一键安装包不包含真实 API key。
- STT / TTS / Fast LLM 访问仍通过 EchoDesk backend 的配置走 eight endpoint。
- 如果后续要给外部客户长期使用，应改为 release 签名 APK / AAB，并配置 HTTPS backend。

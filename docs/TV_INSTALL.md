# EchoDesk 智能电视安装

当前 TV 版本面向 Android TV、Google TV、以及常见国产 Android / AOSP 智能电视。
这类电视通常有「我的应用」入口，允许安装 APK，或可以打开 ADB 网络调试。

## 下载

从 GitHub Release 下载：

- `EchoDesk-0.2.23-smart-tv.apk`：电视直接安装用 APK。
- `EchoDesk-0.2.23-smart-tv-oneclick.zip`：电脑一键安装包，含 APK、macOS 脚本、Windows PowerShell 脚本。
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
4. 解压 `EchoDesk-0.2.23-smart-tv-oneclick.zip`。
5. macOS：

```bash
./install-tv-macos.sh 192.168.1.23
```

6. Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-tv-windows.ps1 -TvIp 192.168.1.23
```

如果电视弹出 RSA 调试授权，先选择允许，再重新运行脚本。

一键脚本默认会执行干净安装：先停止旧 app、清理旧 WebView / app data，再安装、
授权麦克风并自动打开 EchoDesk。这样新安装不会继承上一版的本地缓存。
当前 TV 包名是 `com.echodesk.tv`，和 Android 手机 / 平板包 `com.echodesk.app` 分离；
一键脚本默认还会卸载旧 TV 遗留包 `com.echodesk.app`，避免会议历史串包。
如果只是升级且需要保留旧配置，可在运行前设置：

```bash
ECHODESK_TV_KEEP_DATA=1 ./install-tv-macos.sh 192.168.1.23
```

如果你明确需要保留旧 `com.echodesk.app` 包，可额外设置 `ECHODESK_TV_KEEP_LEGACY=1`。

应用内「检查更新」会直接打开最新 TV APK 下载地址；Android / TV 系统会弹出安装确认。
APK 侧载升级默认保留 app 数据；只有一键安装脚本的默认首次安装模式会清理旧缓存。

## 方法 C：U 盘安装

1. 把 `EchoDesk-0.2.23-smart-tv.apk` 拷到 U 盘。
2. 在电视文件管理器里打开 APK。
3. 按提示允许安装未知来源应用。

## 后端连接

TV APK 是 EchoDesk 前端客户端，不包含 Python backend，也不包含真实 API key。
默认连接 EchoDesk 公网 demo backend：

```text
https://echodesk.yoliyoli.uk
```

模型服务（STT / TTS / Fast LLM）在 eight 上，客户端只连 EchoDesk backend；
真实 key 只在服务端配置，不会打进 APK。

电视端不内置 Python backend：Android TV / AOSP TV 的系统权限、Python wheel、
本地文件目录和后台保活都不适合直接运行完整 FastAPI 后端。TV APK 只负责 UI、
录音、扫码保存和把音频发送到 EchoDesk backend；STT / TTS / LLM 都由 backend
转发到 eight endpoint。这样用户能直接安装使用，同时不会从 APK 里逆向出模型 key。

如需内网调试，也可以临时改成局域网 backend。先在电脑上启动：

```bash
cd backend
source .venv/bin/activate
ECHO_LAN_FULL_API_ENABLED=true python -m uvicorn app.main:app --host 0.0.0.0 --port 8769
```

然后在电视端 EchoDesk 设置里填电脑局域网地址，例如：

```text
http://192.168.1.20:8769
```

## 兼容边界

- 可安装：Android TV / Google TV / 国产 Android 或 AOSP 智能电视 / Android 电视盒子。
- 不可直接安装 APK：Samsung Tizen、LG webOS、Apple TV。
- 非 Android 电视要使用 EchoDesk，建议接一个 Android TV 盒子，或走后续 PWA / 浏览器版本。
- 录音：EchoDesk 会优先使用原生 Android `AudioRecord` 采音；如果电视系统没有向三方
  app 暴露有效输入，或 logcat 里出现 `cannot open pcm_in driver`，应用会停止上传静音块并
  提示接入 USB / 蓝牙会议麦克风。这类情况不是 STT/TTS 服务异常，而是电视音频输入设备不可用。
- 会议室远场：建议使用 USB 全向麦、蓝牙会议麦或带 UAC 的会议摄像头；多数电视内置遥控器麦克风
  只给系统语音助手使用，不一定开放给侧载应用。

## 安全说明

- APK 与一键安装包不包含真实 API key。
- STT / TTS / Fast LLM 访问通过 EchoDesk backend 的配置走 eight endpoint。
- TV 包使用独立 Android 包名 `com.echodesk.tv`；手机 / 平板包继续使用 `com.echodesk.app`。
- 桌面端扫码保存默认只向局域网开放分享页、纪要下载和产物下载；完整 API 需显式打开
  `ECHO_LAN_FULL_API_ENABLED=true`。
- 公网 demo backend 已走 HTTPS；后续正式客户分发还需要 release 签名 APK / AAB、设备注册和限流。

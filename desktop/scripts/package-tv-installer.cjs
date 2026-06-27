#!/usr/bin/env node

const {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = join(__dirname, "..");
const RELEASE_DIR = join(ROOT, "release");
const { version } = require(join(ROOT, "package.json"));

const SOURCE_APK = join(RELEASE_DIR, `EchoDesk-${version}-android-tv.apk`);
const SMART_TV_APK_NAME = `EchoDesk-${version}-smart-tv.apk`;
const SMART_TV_APK = join(RELEASE_DIR, SMART_TV_APK_NAME);
const BUNDLE_NAME = `EchoDesk-${version}-smart-tv-oneclick`;
const BUNDLE_DIR = join(RELEASE_DIR, BUNDLE_NAME);
const BUNDLE_ZIP = join(RELEASE_DIR, `${BUNDLE_NAME}.zip`);
const INSTALL_PAGE_URL =
  process.env.ECHODESK_TV_INSTALL_URL || "http://10.10.12.117:18080/tv.html";

function fail(message) {
  console.error(`[tv-package] ${message}`);
  process.exit(1);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || ROOT,
    env: process.env,
    stdio: options.stdio || "inherit",
    shell: false,
  });
  return result.status === 0;
}

if (!existsSync(SOURCE_APK)) {
  fail(`Missing ${SOURCE_APK}. Run "npm run app:dist:android" first.`);
}

mkdirSync(RELEASE_DIR, { recursive: true });
copyFileSync(SOURCE_APK, SMART_TV_APK);

rmSync(BUNDLE_DIR, { recursive: true, force: true });
rmSync(BUNDLE_ZIP, { force: true });
mkdirSync(BUNDLE_DIR, { recursive: true });

copyFileSync(SMART_TV_APK, join(BUNDLE_DIR, SMART_TV_APK_NAME));

const macInstaller = `#!/usr/bin/env bash
set -euo pipefail

TV_IP="\${1:-}"
if [ -z "$TV_IP" ]; then
  read -r -p "请输入电视 IP（例如 192.168.1.23）: " TV_IP
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APK="$SCRIPT_DIR/${SMART_TV_APK_NAME}"
PKG="com.echodesk.tv"
LEGACY_PKG="com.echodesk.app"

if command -v adb >/dev/null 2>&1; then
  ADB="$(command -v adb)"
elif [ -x "$HOME/Library/Android/sdk/platform-tools/adb" ]; then
  ADB="$HOME/Library/Android/sdk/platform-tools/adb"
elif [ -n "\${ANDROID_HOME:-}" ] && [ -x "$ANDROID_HOME/platform-tools/adb" ]; then
  ADB="$ANDROID_HOME/platform-tools/adb"
else
  echo "没有找到 adb。请安装 Android Studio，或把 platform-tools/adb 加入 PATH。"
  exit 1
fi

echo "[EchoDesk TV] adb=$ADB"
echo "[EchoDesk TV] connecting to $TV_IP:5555 ..."
"$ADB" connect "$TV_IP:5555" || true
SERIAL="$TV_IP:5555"
ADB_DEVICE=("$ADB" -s "$SERIAL")

if [ "\${ECHODESK_TV_KEEP_DATA:-0}" != "1" ]; then
  echo "[EchoDesk TV] clearing old local WebView / app data ..."
  "\${ADB_DEVICE[@]}" shell am force-stop "$PKG" >/dev/null 2>&1 || true
  "\${ADB_DEVICE[@]}" shell pm clear "$PKG" >/dev/null 2>&1 || true
  if [ "\${ECHODESK_TV_KEEP_LEGACY:-0}" != "1" ]; then
    "\${ADB_DEVICE[@]}" shell am force-stop "$LEGACY_PKG" >/dev/null 2>&1 || true
    "\${ADB_DEVICE[@]}" shell pm clear "$LEGACY_PKG" >/dev/null 2>&1 || true
    "\${ADB_DEVICE[@]}" shell pm uninstall "$LEGACY_PKG" >/dev/null 2>&1 || true
  fi
fi

echo "[EchoDesk TV] installing $APK ..."
"\${ADB_DEVICE[@]}" install -r -d "$APK"

"\${ADB_DEVICE[@]}" shell pm grant "$PKG" android.permission.RECORD_AUDIO >/dev/null 2>&1 || true
"\${ADB_DEVICE[@]}" shell appops set "$PKG" RECORD_AUDIO allow >/dev/null 2>&1 || true
"\${ADB_DEVICE[@]}" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true

echo
echo "安装完成，已尝试自动打开 EchoDesk。"
echo "如果电视弹出调试授权，请先在电视上点允许，再重新运行本脚本。"
echo "如需保留旧配置更新，请用：ECHODESK_TV_KEEP_DATA=1 ./install-tv-macos.sh $TV_IP"
echo "如需保留旧 com.echodesk.app 包，请同时设置：ECHODESK_TV_KEEP_LEGACY=1"
`;

const winInstaller = `param(
  [string]$TvIp
)

if (-not $TvIp) {
  $TvIp = Read-Host "请输入电视 IP（例如 192.168.1.23）"
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Apk = Join-Path $ScriptDir "${SMART_TV_APK_NAME}"
$Pkg = "com.echodesk.tv"
$LegacyPkg = "com.echodesk.app"

$Candidates = @(
  "adb.exe",
  "$env:LOCALAPPDATA\\Android\\Sdk\\platform-tools\\adb.exe",
  "$env:ANDROID_HOME\\platform-tools\\adb.exe",
  "$env:ANDROID_SDK_ROOT\\platform-tools\\adb.exe"
)

$Adb = $null
foreach ($Candidate in $Candidates) {
  if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
    $Adb = (Get-Command $Candidate).Source
    break
  }
}

if (-not $Adb) {
  Write-Error "没有找到 adb。请安装 Android Studio，或把 platform-tools\\adb.exe 加入 PATH。"
  exit 1
}

Write-Host "[EchoDesk TV] adb=$Adb"
Write-Host "[EchoDesk TV] connecting to $($TvIp):5555 ..."
& $Adb connect "$($TvIp):5555"
$Serial = "$($TvIp):5555"

if ($env:ECHODESK_TV_KEEP_DATA -ne "1") {
  Write-Host "[EchoDesk TV] clearing old local WebView / app data ..."
  & $Adb -s $Serial shell am force-stop $Pkg | Out-Null
  & $Adb -s $Serial shell pm clear $Pkg | Out-Null
  if ($env:ECHODESK_TV_KEEP_LEGACY -ne "1") {
    & $Adb -s $Serial shell am force-stop $LegacyPkg | Out-Null
    & $Adb -s $Serial shell pm clear $LegacyPkg | Out-Null
    & $Adb -s $Serial shell pm uninstall $LegacyPkg | Out-Null
  }
}

Write-Host "[EchoDesk TV] installing $Apk ..."
& $Adb -s $Serial install -r -d "$Apk"

$null = & $Adb -s $Serial shell pm grant $Pkg android.permission.RECORD_AUDIO
$null = & $Adb -s $Serial shell appops set $Pkg RECORD_AUDIO allow
$null = & $Adb -s $Serial shell monkey -p $Pkg -c android.intent.category.LAUNCHER 1

Write-Host ""
Write-Host "安装完成，已尝试自动打开 EchoDesk。"
Write-Host "如果电视弹出调试授权，请先在电视上点允许，再重新运行本脚本。"
Write-Host "如需保留旧配置更新，请先设置 ECHODESK_TV_KEEP_DATA=1。"
Write-Host "如需保留旧 com.echodesk.app 包，请同时设置 ECHODESK_TV_KEEP_LEGACY=1。"
`;

const readme = `EchoDesk 智能电视一键安装包 ${version}

适用范围
- Android TV / Google TV / 国产 Android 或 AOSP 智能电视。
- 电视桌面有「我的应用」或允许安装 APK 时，安装后会显示 EchoDesk。
- Samsung Tizen、LG webOS、Apple TV 不能安装 APK；这类设备只能走浏览器/PWA 或外接 Android 盒子。

一键安装（推荐）
1. 让电脑和电视在同一个局域网。
2. 在电视设置里打开「开发者模式」和「网络调试 / ADB 调试」。
3. 查到电视 IP。
4. macOS 运行：
   ./install-tv-macos.sh 电视IP
5. Windows PowerShell 运行：
   powershell -ExecutionPolicy Bypass -File .\\install-tv-windows.ps1 -TvIp 电视IP
6. 脚本默认清理旧的本地 WebView / app data，授权麦克风并自动打开 EchoDesk。
   如需保留旧配置更新，设置 ECHODESK_TV_KEEP_DATA=1 后再运行。
   新 TV 版包名是 com.echodesk.tv，默认会卸载旧 com.echodesk.app 电视遗留包，避免历史数据串包。
   如需保留旧包，额外设置 ECHODESK_TV_KEEP_LEGACY=1。
7. 如果电视弹出 RSA 调试授权，选择允许，再重新运行脚本。

手动安装
- 把 ${SMART_TV_APK_NAME} 拷到 U 盘，在电视文件管理器中打开安装。
- 或者用电视浏览器打开 GitHub Release 的 APK 下载链接。

使用前提
- APK 只是 EchoDesk 前端壳，不包含 Python backend，也不包含真实 API key。
- 电视端需要能访问 EchoDesk backend。会议室内测建议让 Mac/Windows 后端监听 0.0.0.0:8769，并在 EchoDesk 设置里填电脑局域网地址。
`;

writeFileSync(join(BUNDLE_DIR, "install-tv-macos.sh"), macInstaller, "utf-8");
chmodSync(join(BUNDLE_DIR, "install-tv-macos.sh"), 0o755);
writeFileSync(join(BUNDLE_DIR, "install-tv-windows.ps1"), winInstaller, "utf-8");
writeFileSync(join(BUNDLE_DIR, "README-TV-INSTALL.txt"), readme, "utf-8");

const tvInstallPage = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>EchoDesk TV 安装</title>
    <style>
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #f6f7f8;
        color: #1f2937;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      main {
        width: min(880px, calc(100vw - 64px));
        padding: 48px;
        border: 1px solid #e5e7eb;
        border-radius: 24px;
        background: #fff;
        box-shadow: 0 18px 60px rgba(15, 23, 42, 0.12);
      }
      h1 { margin: 0 0 10px; font-size: 44px; line-height: 1.1; }
      p { margin: 0 0 28px; color: #6b7280; font-size: 22px; line-height: 1.5; }
      a {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 320px;
        min-height: 72px;
        padding: 0 32px;
        border-radius: 18px;
        background: #0f9f77;
        color: #fff;
        font-size: 26px;
        font-weight: 800;
        text-decoration: none;
      }
      a:focus, a:hover { outline: 6px solid rgba(15, 159, 119, 0.22); }
      code {
        display: block;
        margin-top: 24px;
        padding: 18px 20px;
        border-radius: 14px;
        background: #f3f4f6;
        color: #374151;
        font-size: 20px;
        word-break: break-all;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>EchoDesk TV</h1>
      <p>点击下面按钮下载最新版 TV 兼容 APK。安装时如果系统询问是否替换现有应用，选择替换。</p>
      <a href="./${SMART_TV_APK_NAME}" autofocus>下载 TV APK v${version}</a>
      <code>${INSTALL_PAGE_URL}</code>
    </main>
  </body>
</html>
`;
writeFileSync(join(RELEASE_DIR, "tv.html"), tvInstallPage, "utf-8");
copyFileSync(join(ROOT, "..", "docs", "tv-install.html"), join(RELEASE_DIR, "tv-install.html"));
writeFileSync(
  join(RELEASE_DIR, "t"),
  '<!doctype html><meta http-equiv="refresh" content="0; url=/tv.html"><a href="/tv.html">EchoDesk TV</a>\n',
  "utf-8",
);

if (!run("zip", ["-qr", BUNDLE_ZIP, "."], { cwd: BUNDLE_DIR })) {
  fail("zip command failed while creating the TV one-click package.");
}

console.log(`[tv-package] smart TV APK: ${SMART_TV_APK}`);
console.log(`[tv-package] one-click bundle: ${BUNDLE_ZIP}`);

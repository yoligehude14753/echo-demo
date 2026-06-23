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

const SOURCE_APK = join(RELEASE_DIR, `EchoDesk-${version}-android-tv-debug.apk`);
const SMART_TV_APK_NAME = `EchoDesk-${version}-smart-tv.apk`;
const SMART_TV_APK = join(RELEASE_DIR, SMART_TV_APK_NAME);
const BUNDLE_NAME = `EchoDesk-${version}-smart-tv-oneclick`;
const BUNDLE_DIR = join(RELEASE_DIR, BUNDLE_NAME);
const BUNDLE_ZIP = join(RELEASE_DIR, `${BUNDLE_NAME}.zip`);

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

echo "[EchoDesk TV] installing $APK ..."
"$ADB" install -r -d "$APK"

echo
echo "安装完成。请在电视的「我的应用」里打开 EchoDesk。"
echo "如果电视弹出调试授权，请先在电视上点允许，再重新运行本脚本。"
`;

const winInstaller = `param(
  [string]$TvIp
)

if (-not $TvIp) {
  $TvIp = Read-Host "请输入电视 IP（例如 192.168.1.23）"
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Apk = Join-Path $ScriptDir "${SMART_TV_APK_NAME}"

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

Write-Host "[EchoDesk TV] installing $Apk ..."
& $Adb install -r -d "$Apk"

Write-Host ""
Write-Host "安装完成。请在电视的「我的应用」里打开 EchoDesk。"
Write-Host "如果电视弹出调试授权，请先在电视上点允许，再重新运行本脚本。"
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
6. 如果电视弹出 RSA 调试授权，选择允许，再重新运行脚本。

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

if (!run("zip", ["-qr", BUNDLE_ZIP, "."], { cwd: BUNDLE_DIR })) {
  fail("zip command failed while creating the TV one-click package.");
}

console.log(`[tv-package] smart TV APK: ${SMART_TV_APK}`);
console.log(`[tv-package] one-click bundle: ${BUNDLE_ZIP}`);

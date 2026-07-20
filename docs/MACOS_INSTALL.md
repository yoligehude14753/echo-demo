# 在 macOS 安装 EchoDesk 0.3.4

EchoDesk 0.3.4 当前提供 Apple Silicon（arm64）安装包。该版本未经过 Apple notarization，
因此直接双击下载的 App 时，macOS 可能显示“应用已损坏”或“无法验证开发者”。下面三种方式
都只处理 EchoDesk 本身，不关闭全局 Gatekeeper，也不修改其他 App。

## 方式 A：下载 DMG 手动安装

1. 从 [EchoDesk v0.3.4 Release](https://github.com/yoligehude14753/echo-demo/releases/tag/v0.3.4)
   下载 `EchoDesk-0.3.4-arm64.dmg`。
2. 打开 DMG，把 `EchoDesk.app` 拖到“应用程序”。
3. 第一次启动时，在 Finder 的“应用程序”中右键 `EchoDesk`，选择“打开”，再确认一次“打开”。
4. 如果系统仍显示“应用已损坏”，改用方式 B 的 `--no-quarantine` 安装，或直接使用方式 C。

DMG 的 SHA-256：

```text
bda39991d7ce32263624f85ff0e75fcae3e3187af26249a25d811aa03e466eff
```

## 方式 B：使用 Homebrew Cask

需要先安装 [Homebrew](https://brew.sh/)。首次添加 EchoDesk tap：

```bash
brew tap yoligehude14753/echo-demo https://github.com/yoligehude14753/echo-demo
brew install --cask --no-quarantine echodesk
```

后续升级：

```bash
brew update
brew upgrade --cask --no-quarantine echodesk
```

如果提示 `/Applications/EchoDesk.app` 已存在，先把旧 App 移到带时间戳的备份目录，再安装：

```bash
sudo mv "/Applications/EchoDesk.app" "/Applications/EchoDesk.app.backup.$(date +%Y%m%d-%H%M%S)"
brew install --cask --no-quarantine echodesk
```

如果 Homebrew 已登记旧 cask，可先卸载 cask 记录；这不会删除 `~/.echodesk/` 中的用户数据：

```bash
brew uninstall --cask echodesk
brew install --cask --no-quarantine echodesk
```

需要覆盖当前 cask 安装时：

```bash
brew reinstall --cask --no-quarantine --force echodesk
```

## 方式 C：无 Homebrew 的通用终端安装

把下面整段复制到 Terminal 执行。它会下载 v0.3.4 DMG、校验 SHA-256、挂载 DMG，备份已有
`/Applications/EchoDesk.app`，然后复制、只清除 EchoDesk 的 quarantine、本机 ad-hoc 重签、
严格验证并启动。它不会执行远程脚本，也不会关闭 Gatekeeper。

```bash
set -euo pipefail

ECHODESK_VERSION="0.3.4"
ECHODESK_SHA256="bda39991d7ce32263624f85ff0e75fcae3e3187af26249a25d811aa03e466eff"
ECHODESK_URL="https://github.com/yoligehude14753/echo-demo/releases/download/v${ECHODESK_VERSION}/EchoDesk-${ECHODESK_VERSION}-arm64.dmg"
ECHODESK_TMP="$(mktemp -d /tmp/echodesk-install.XXXXXX)"
ECHODESK_DMG="${ECHODESK_TMP}/EchoDesk.dmg"
ECHODESK_MOUNT="${ECHODESK_TMP}/mnt"

mkdir -p "${ECHODESK_MOUNT}"
cleanup_echodesk_install() {
  hdiutil detach "${ECHODESK_MOUNT}" >/dev/null 2>&1 || true
  rm -rf "${ECHODESK_TMP}"
}
trap cleanup_echodesk_install EXIT

curl --fail --location --retry 3 --output "${ECHODESK_DMG}" "${ECHODESK_URL}"
printf '%s  %s\n' "${ECHODESK_SHA256}" "${ECHODESK_DMG}" | shasum -a 256 --check
hdiutil attach "${ECHODESK_DMG}" -nobrowse -readonly -mountpoint "${ECHODESK_MOUNT}"

if [ -d "/Applications/EchoDesk.app" ]; then
  sudo mv "/Applications/EchoDesk.app" "/Applications/EchoDesk.app.backup.$(date +%Y%m%d-%H%M%S)"
fi

sudo ditto "${ECHODESK_MOUNT}/EchoDesk.app" "/Applications/EchoDesk.app"
sudo xattr -dr com.apple.quarantine "/Applications/EchoDesk.app"
sudo codesign --force --deep --sign - "/Applications/EchoDesk.app"
codesign --verify --deep --strict --verbose=2 "/Applications/EchoDesk.app"
open "/Applications/EchoDesk.app"
```

第一次使用时请允许麦克风权限。用户数据位于 `~/.echodesk/`；上述安装或覆盖步骤不会删除该目录。

## 卸载 App

使用 Homebrew 安装时：

```bash
brew uninstall --cask echodesk
```

手动安装时，把 `/Applications/EchoDesk.app` 移到废纸篓即可。若还要删除本机数据，请先确认
不再需要会议、知识库和配置，再自行备份并移除 `~/.echodesk/`。

#!/usr/bin/env node
/* eslint-disable @typescript-eslint/no-var-requires */
/**
 * Dev 模式品牌补丁。
 *
 * 背景：dev 模式跑 `electron electron/main.cjs` 时，macOS Dock / Cmd+Tab 显示的进程名
 * 来自 `node_modules/electron/dist/Electron.app/Contents/Info.plist` 的 CFBundleName / CFBundleIconFile，
 * 跟 `app.setName()` 无关。所以要让 dev 模式 Dock 显示 "Echo (dev)" + 自定义图标，
 * 必须就地补丁 node_modules 里的 Electron.app。
 *
 * 操作：
 *   1. 改 Info.plist 的 CFBundleName / CFBundleDisplayName / CFBundleIconFile
 *   2. 把 desktop/electron/icons/echo.icns 拷成 Resources/echo.icns
 *
 * 重装 electron 会重置 Info.plist + 删 icon，所以 package.json 的 postinstall 也会调本脚本。
 *
 * Idempotent：重复调用安全。
 */
const fs = require("node:fs");
const path = require("node:path");
const { execSync } = require("node:child_process");

const APP_NAME = "Echo (dev)";
const ICON_FILE = "echo.icns";
const ROOT = path.resolve(__dirname, "..", "..");
const ELECTRON_APP = path.join(
  ROOT,
  "node_modules",
  "electron",
  "dist",
  "Electron.app",
);
const INFO_PLIST = path.join(ELECTRON_APP, "Contents", "Info.plist");
const RES_DIR = path.join(ELECTRON_APP, "Contents", "Resources");
const SRC_ICON = path.join(__dirname, "..", "icons", ICON_FILE);

function fail(msg) {
  console.error(`[brand-dev-electron] ${msg}`);
  process.exit(0); // 不让 install 因为这个 fail（仅警告）
}

function main() {
  if (process.platform !== "darwin") {
    console.log("[brand-dev-electron] 非 macOS 跳过");
    return;
  }
  if (!fs.existsSync(ELECTRON_APP)) {
    fail(`Electron.app 未找到（${ELECTRON_APP}），跳过`);
    return;
  }
  if (!fs.existsSync(SRC_ICON)) {
    fail(`icon 未找到（${SRC_ICON}），跳过`);
    return;
  }

  // 1. 拷贝 icon
  const targetIcon = path.join(RES_DIR, ICON_FILE);
  fs.copyFileSync(SRC_ICON, targetIcon);
  console.log(`[brand-dev-electron] ✓ icon → ${targetIcon}`);

  // 2. 改 Info.plist
  // 用 PlistBuddy（macOS 自带，最稳定的方式）
  const plistBuddy = "/usr/libexec/PlistBuddy";
  const setOrAdd = (key, value) => {
    try {
      execSync(`${plistBuddy} -c "Set :${key} '${value}'" "${INFO_PLIST}"`, {
        stdio: "pipe",
      });
    } catch {
      execSync(
        `${plistBuddy} -c "Add :${key} string '${value}'" "${INFO_PLIST}"`,
        { stdio: "pipe" },
      );
    }
  };
  setOrAdd("CFBundleName", APP_NAME);
  setOrAdd("CFBundleDisplayName", APP_NAME);
  setOrAdd("CFBundleIconFile", ICON_FILE);
  setOrAdd("CFBundleIconName", ICON_FILE.replace(/\.icns$/, ""));
  // 让 macOS 重新读 bundle 元数据
  try {
    execSync(`touch "${ELECTRON_APP}"`, { stdio: "ignore" });
    // 清 Launch Services 缓存（旧名残留）
    execSync(
      `/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "${ELECTRON_APP}"`,
      { stdio: "ignore" },
    );
  } catch {
    /* lsregister 不一定成功，不强求 */
  }
  console.log(`[brand-dev-electron] ✓ Info.plist patched: ${APP_NAME}`);
}

main();

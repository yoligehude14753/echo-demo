const { existsSync } = require("node:fs");
const { join } = require("node:path");
const { execFileSync } = require("node:child_process");

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }

  const productName = context.packager.appInfo.productFilename;
  const appPath = join(context.appOutDir, `${productName}.app`);
  if (!existsSync(appPath)) {
    throw new Error(`[mac-sign] Missing packaged app: ${appPath}`);
  }

  console.log(`[mac-sign] ad-hoc signing ${appPath}`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", appPath], {
    stdio: "inherit",
  });
  execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath], {
    stdio: "inherit",
  });
};

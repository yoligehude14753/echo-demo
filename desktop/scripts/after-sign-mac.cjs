/* eslint-disable @typescript-eslint/no-var-requires */
"use strict";

const { existsSync } = require("node:fs");
const { join } = require("node:path");

const { verifyMacBundle } = require("./mac-bundle-sign.cjs");

module.exports = async function afterSign(context) {
  if (context.electronPlatformName !== "darwin") return;

  const productName = context.packager.appInfo.productFilename;
  const appPath = join(context.appOutDir, `${productName}.app`);
  if (!existsSync(appPath)) {
    throw new Error(`[mac-sign] Missing signed packaged app: ${appPath}`);
  }
  verifyMacBundle(appPath);
};

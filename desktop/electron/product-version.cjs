"use strict";

const fs = require("node:fs");

function resolveDesktopProductVersion(packageJsonPath) {
  if (typeof packageJsonPath !== "string" || !packageJsonPath.trim()) {
    throw new Error("desktop package.json path is required");
  }
  let packageJson;
  try {
    packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
  } catch (error) {
    throw new Error(`desktop package.json could not be read: ${packageJsonPath}`, {
      cause: error,
    });
  }
  const version = typeof packageJson.version === "string" ? packageJson.version.trim() : "";
  if (!version) {
    throw new Error(`desktop package.json has no version: ${packageJsonPath}`);
  }
  return version;
}

module.exports = { resolveDesktopProductVersion };

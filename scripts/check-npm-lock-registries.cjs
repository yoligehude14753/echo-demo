"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const lockfiles = [
  "desktop/package-lock.json",
  "backend/app/adapters/skill/assets/ppt_ib_deck/package-lock.json",
];
const allowedPrefix = "https://registry.npmjs.org/";
const violations = [];

for (const relative of lockfiles) {
  const absolute = path.join(root, relative);
  const lock = JSON.parse(fs.readFileSync(absolute, "utf8"));
  for (const [packagePath, entry] of Object.entries(lock.packages ?? {})) {
    const resolved = entry && typeof entry === "object" ? entry.resolved : undefined;
    if (typeof resolved === "string" && !resolved.startsWith(allowedPrefix)) {
      violations.push(`${relative}:${packagePath || "<root>"}: ${resolved}`);
    }
  }
}

if (violations.length > 0) {
  console.error("npm lockfiles contain non-official package URLs:\n" + violations.join("\n"));
  process.exit(1);
}

console.log(`npm lock registry gate passed for ${lockfiles.length} lockfiles`);

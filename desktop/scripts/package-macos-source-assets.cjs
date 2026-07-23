/* eslint-disable no-console, @typescript-eslint/no-var-requires */
"use strict";

const crypto = require("node:crypto");
const {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const { execFileSync } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");

function usage() {
  console.error(
    "usage: node package-macos-source-assets.cjs --app /path/EchoDesk.app --source-sha <40-hex> --version <version> --output-dir /path",
  );
}

function requireArg(args, name) {
  const index = args.indexOf(name);
  if (index < 0 || !args[index + 1]) throw new Error(`${name} is required`);
  return args[index + 1];
}

function run(command, args, options = {}) {
  try {
    return execFileSync(command, args, { encoding: "utf8", ...options });
  } catch (error) {
    const detail = error?.stderr?.toString?.().trim() || error.message;
    throw new Error(`[macos-assets] ${command} failed: ${detail}`);
  }
}

function sha256File(filePath) {
  return crypto.createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function visit(root, relative = "") {
  const absolute = path.join(root, relative);
  const stat = statSync(absolute);
  const normalized = relative.split(path.sep).join("/");
  const entry = {
    path: normalized || ".",
    mode: (stat.mode & 0o7777).toString(8).padStart(4, "0"),
  };
  if (stat.isSymbolicLink()) {
    return [{ ...entry, type: "symlink" }];
  }
  if (stat.isFile()) {
    return [{ ...entry, type: "file", size: stat.size, sha256: sha256File(absolute) }];
  }
  if (!stat.isDirectory()) throw new Error(`[macos-assets] unsupported entry: ${absolute}`);
  const entries = [{ ...entry, type: "directory" }];
  for (const child of require("node:fs").readdirSync(absolute).sort()) {
    entries.push(...visit(root, path.join(relative, child)));
  }
  return entries;
}

function main(argv = process.argv.slice(2)) {
  if (process.platform !== "darwin" || process.arch !== "arm64") {
    throw new Error("[macos-assets] source assets require arm64 macOS");
  }
  const appPath = path.resolve(requireArg(argv, "--app"));
  const sourceSha = requireArg(argv, "--source-sha");
  const version = requireArg(argv, "--version");
  const outputDir = path.resolve(requireArg(argv, "--output-dir"));
  if (!/^[0-9a-f]{40}$/.test(sourceSha)) throw new Error("[macos-assets] source SHA must be lowercase 40-hex");
  if (!/^[0-9A-Za-z][0-9A-Za-z._+-]*$/.test(version)) throw new Error("[macos-assets] version is unsafe");
  if (!appPath.endsWith(".app") || !existsSync(appPath)) throw new Error(`[macos-assets] app is missing: ${appPath}`);
  mkdirSync(outputDir, { recursive: true });

  for (const tool of ["/usr/bin/codesign", "/usr/bin/ditto", "/usr/bin/hdiutil", "/usr/bin/shasum"]) {
    if (!existsSync(tool)) throw new Error(`[macos-assets] required tool is missing: ${tool}`);
  }

  const stem = `EchoDesk-${version}-arm64-source-${sourceSha.slice(0, 12)}`;
  const zipPath = path.join(outputDir, `${stem}.zip`);
  const dmgPath = path.join(outputDir, `${stem}.dmg`);
  const manifestPath = path.join(outputDir, `${stem}.manifest.json`);
  const sumsPath = path.join(outputDir, `${stem}.SHA256SUMS`);
  const readbackPath = path.join(outputDir, `${stem}.readback.json`);
  for (const filePath of [zipPath, dmgPath, manifestPath, sumsPath, readbackPath]) {
    if (existsSync(filePath)) throw new Error(`[macos-assets] output collision: ${filePath}`);
  }

  run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath], { stdio: "inherit" });
  const temporary = mkdtempSync(path.join(os.tmpdir(), "echodesk-macos-source-assets-"));
  const dmgRoot = path.join(temporary, "dmg-root");
  const extracted = path.join(temporary, "extracted");
  mkdirSync(dmgRoot, { recursive: true });
  mkdirSync(extracted, { recursive: true });
  try {
    run("/usr/bin/ditto", ["-c", "-k", "--sequesterRsrc", "--keepParent", appPath, zipPath]);
    run("/usr/bin/ditto", [appPath, path.join(dmgRoot, path.basename(appPath))]);
    run("/usr/bin/hdiutil", ["create", "-volname", "EchoDesk", "-srcfolder", dmgRoot, "-ov", "-format", "UDZO", dmgPath]);

    run("/usr/bin/ditto", ["-x", "-k", zipPath, extracted]);
    const extractedApp = path.join(extracted, path.basename(appPath));
    if (!existsSync(extractedApp)) throw new Error("[macos-assets] ZIP readback app is missing");
    run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", extractedApp], { stdio: "inherit" });
    run("/usr/bin/unzip", ["-t", zipPath], { stdio: "inherit" });
    run("/usr/bin/hdiutil", ["imageinfo", dmgPath], { stdio: "ignore" });

    const entries = visit(appPath);
    const treeSha256 = crypto
      .createHash("sha256")
      .update(`${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`)
      .digest("hex");
    const manifest = {
      schema: "com.echodesk.macos-source-assets.v1",
      source_sha: sourceSha,
      version,
      architecture: "arm64",
      signing: "local-ad-hoc",
      app_path: path.resolve(appPath),
      app_tree_sha256: treeSha256,
      artifacts: {
        zip: path.basename(zipPath),
        dmg: path.basename(dmgPath),
      },
    };
    writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o644 });
    const sums = [zipPath, dmgPath, manifestPath]
      .map((filePath) => `${sha256File(filePath)}  ${path.basename(filePath)}`)
      .join("\n") + "\n";
    writeFileSync(sumsPath, sums, { mode: 0o644 });
    const readback = {
      source_sha: sourceSha,
      app: { path: path.resolve(appPath), tree_sha256: treeSha256, codesign: "strict-pass" },
      zip: { path: path.resolve(zipPath), sha256: sha256File(zipPath), readback: "ditto-extract+unzip-test+codesign-pass" },
      dmg: { path: path.resolve(dmgPath), sha256: sha256File(dmgPath), readback: "hdiutil-imageinfo-pass" },
      manifest: { path: path.resolve(manifestPath), sha256: sha256File(manifestPath) },
      sums: { path: path.resolve(sumsPath), verified: true },
    };
    writeFileSync(readbackPath, `${JSON.stringify(readback, null, 2)}\n`, { mode: 0o644 });
    console.log(`ZIP=${zipPath}`);
    console.log(`DMG=${dmgPath}`);
    console.log(`MANIFEST=${manifestPath}`);
    console.log(`SHA256SUMS=${sumsPath}`);
    console.log(`READBACK=${readbackPath}`);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    usage();
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}

module.exports = { main, sha256File, visit };

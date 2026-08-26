/* eslint-disable no-console */
const {
  existsSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} = require("node:fs");
const path = require("node:path");
const yaml = require("js-yaml");
const {
  buildBlockMap,
} = require("app-builder-lib/out/targets/blockmap/blockmap");

const DESKTOP_ROOT = path.resolve(__dirname, "..");

function requiredFile(filePath, label) {
  if (!existsSync(filePath)) {
    throw new Error(`[mac-update-metadata] Missing ${label}: ${filePath}`);
  }
  return filePath;
}

function findFileEntry(metadata, filename) {
  if (!Array.isArray(metadata.files)) {
    throw new Error("[mac-update-metadata] latest-mac.yml files must be an array");
  }
  const matches = metadata.files.filter(
    (entry) => entry && entry.url === filename,
  );
  if (matches.length !== 1) {
    throw new Error(
      `[mac-update-metadata] latest-mac.yml must contain exactly one ${filename} entry`,
    );
  }
  return matches[0];
}

async function refreshMacUpdateMetadata(desktopRoot = DESKTOP_ROOT) {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const version = String(pkg.version || "").trim();
  if (!version) {
    throw new Error("[mac-update-metadata] package version is missing");
  }

  const releaseRoot = path.join(desktopRoot, "release");
  const metadataPath = requiredFile(
    path.join(releaseRoot, "latest-mac.yml"),
    "macOS update metadata",
  );
  const filenames = [
    `EchoDesk-${version}-arm64-mac.zip`,
    `EchoDesk-${version}-arm64.dmg`,
  ];
  const metadata = yaml.load(readFileSync(metadataPath, "utf8"));
  if (!metadata || typeof metadata !== "object" || metadata.version !== version) {
    throw new Error(
      `[mac-update-metadata] latest-mac.yml version does not match ${version}`,
    );
  }

  const updateInfo = new Map();
  for (const filename of filenames) {
    const artifactPath = requiredFile(
      path.join(releaseRoot, filename),
      `macOS updater artifact ${filename}`,
    );
    const info = await buildBlockMap(
      artifactPath,
      "gzip",
      `${artifactPath}.blockmap`,
    );
    if (
      !Number.isSafeInteger(info.size) ||
      info.size <= 0 ||
      typeof info.sha512 !== "string" ||
      !info.sha512
    ) {
      throw new Error(
        `[mac-update-metadata] invalid final update info for ${filename}`,
      );
    }
    updateInfo.set(filename, info);
  }

  for (const filename of filenames) {
    const entry = findFileEntry(metadata, filename);
    const info = updateInfo.get(filename);
    entry.sha512 = info.sha512;
    entry.size = info.size;
  }

  const zipName = filenames[0];
  const zipInfo = updateInfo.get(zipName);
  if (metadata.path !== zipName) {
    throw new Error(
      `[mac-update-metadata] latest-mac.yml path must remain ${zipName}`,
    );
  }
  metadata.sha512 = zipInfo.sha512;

  const temporaryPath = `${metadataPath}.tmp-${process.pid}`;
  try {
    writeFileSync(
      temporaryPath,
      yaml.dump(metadata, {
        lineWidth: -1,
        noRefs: true,
        sortKeys: false,
      }),
      { encoding: "utf8", mode: 0o644 },
    );
    renameSync(temporaryPath, metadataPath);
  } finally {
    rmSync(temporaryPath, { force: true });
  }

  for (const filename of filenames) {
    requiredFile(
      path.join(releaseRoot, `${filename}.blockmap`),
      `final blockmap ${filename}.blockmap`,
    );
  }
  console.log(
    `[mac-update-metadata] refreshed final blockmaps and latest-mac.yml for ${version}`,
  );
  return { metadataPath, updateInfo };
}

if (require.main === module) {
  refreshMacUpdateMetadata().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}

module.exports = { refreshMacUpdateMetadata };

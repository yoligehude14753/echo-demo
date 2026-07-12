/* eslint-disable no-console */
const { createHash } = require("node:crypto");
const {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { gunzipSync } = require("node:zlib");
const yaml = require("js-yaml");
const {
  buildBlockMap,
} = require("app-builder-lib/out/targets/blockmap/blockmap");

const DESKTOP_ROOT = path.resolve(__dirname, "..");
const MAX_COMPRESSED_BLOCKMAP_BYTES = 16 * 1024 * 1024;
const MAX_DECOMPRESSED_BLOCKMAP_BYTES = 64 * 1024 * 1024;

function requiredFile(filePath, label) {
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    throw new Error(`[update-metadata] Missing ${label}: ${filePath}`);
  }
  return filePath;
}

function sha512(filePath) {
  return createHash("sha512").update(readFileSync(filePath)).digest("base64");
}

function readExternalBlockmap(filePath, filename) {
  const compressedSize = statSync(filePath).size;
  if (
    compressedSize < 1 ||
    compressedSize > MAX_COMPRESSED_BLOCKMAP_BYTES
  ) {
    throw new Error(
      `[update-metadata] ${filename} compressed size is outside the allowed range`,
    );
  }
  try {
    return gunzipSync(readFileSync(filePath), {
      maxOutputLength: MAX_DECOMPRESSED_BLOCKMAP_BYTES,
    });
  } catch {
    throw new Error(
      `[update-metadata] ${filename} is not a valid bounded gzip blockmap`,
    );
  }
}

function contract(target, version) {
  if (target === "mac") {
    const zip = `EchoDesk-${version}-arm64-mac.zip`;
    const dmg = `EchoDesk-${version}-arm64.dmg`;
    return {
      metadata: "latest-mac.yml",
      files: [zip, dmg],
      primary: zip,
      blockmaps: [`${zip}.blockmap`, `${dmg}.blockmap`],
    };
  }
  if (target === "windows") {
    const installer = `EchoDesk.Setup.${version}.exe`;
    return {
      metadata: "latest.yml",
      files: [installer],
      primary: installer,
      blockmaps: [`${installer}.blockmap`],
    };
  }
  if (target === "linux") {
    const appImage = `EchoDesk-${version}-linux-x86_64.AppImage`;
    const deb = `EchoDesk-${version}-linux-amd64.deb`;
    return {
      metadata: "latest-linux.yml",
      files: [appImage, deb],
      primary: appImage,
      blockmaps: [],
      embeddedBlockmaps: [appImage],
    };
  }
  throw new Error("[update-metadata] target must be mac, windows, or linux");
}

async function verifyEmbeddedBlockmap(
  artifactPath,
  entry,
  blockmapRoot,
  filename,
) {
  const blockMapSize = entry.blockMapSize;
  const artifactBytes = readFileSync(artifactPath);
  if (
    !Number.isSafeInteger(blockMapSize) ||
    blockMapSize < 1 ||
    blockMapSize + 4 >= artifactBytes.length
  ) {
    throw new Error(
      `[update-metadata] ${filename} has an invalid embedded blockmap size`,
    );
  }
  const footerSize = artifactBytes.readUInt32BE(artifactBytes.length - 4);
  if (footerSize !== blockMapSize) {
    throw new Error(
      `[update-metadata] ${filename} embedded blockmap footer does not match metadata`,
    );
  }

  const prefixSize = artifactBytes.length - blockMapSize - 4;
  const recomputedPath = path.join(
    blockmapRoot,
    `${path.basename(filename)}.without-blockmap`,
  );
  writeFileSync(recomputedPath, artifactBytes.subarray(0, prefixSize), {
    mode: 0o600,
  });
  await buildBlockMap(recomputedPath, "deflate");
  const recomputedBytes = readFileSync(recomputedPath);
  if (
    !artifactBytes
      .subarray(prefixSize)
      .equals(recomputedBytes.subarray(prefixSize))
  ) {
    throw new Error(
      `[update-metadata] ${filename} embedded blockmap does not match final artifact bytes`,
    );
  }
}

async function verifyReleaseUpdateMetadata(target, desktopRoot = DESKTOP_ROOT) {
  const pkg = JSON.parse(
    readFileSync(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const version = String(pkg.version || "").trim();
  if (!version) {
    throw new Error("[update-metadata] package version is missing");
  }
  const expected = contract(target, version);
  const releaseRoot = path.join(desktopRoot, "release");
  const metadataPath = requiredFile(
    path.join(releaseRoot, expected.metadata),
    `${target} update metadata`,
  );
  const metadata = yaml.load(readFileSync(metadataPath, "utf8"));
  if (!metadata || typeof metadata !== "object") {
    throw new Error(`[update-metadata] ${expected.metadata} is not an object`);
  }
  if (metadata.version !== version) {
    throw new Error(
      `[update-metadata] ${expected.metadata} version ${String(metadata.version)} does not match ${version}`,
    );
  }
  if (!Array.isArray(metadata.files)) {
    throw new Error(`[update-metadata] ${expected.metadata} files must be an array`);
  }
  const entries = new Map();
  for (const entry of metadata.files) {
    if (!entry || typeof entry !== "object" || typeof entry.url !== "string") {
      throw new Error(`[update-metadata] ${expected.metadata} has an invalid file entry`);
    }
    if (entries.has(entry.url)) {
      throw new Error(`[update-metadata] duplicate metadata URL: ${entry.url}`);
    }
    entries.set(entry.url, entry);
  }
  const actualNames = [...entries.keys()].sort();
  const expectedNames = [...expected.files].sort();
  if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
    throw new Error(
      `[update-metadata] ${expected.metadata} URLs ${JSON.stringify(actualNames)} ` +
        `do not match ${JSON.stringify(expectedNames)}`,
    );
  }

  for (const filename of expected.files) {
    const artifactPath = requiredFile(
      path.join(releaseRoot, filename),
      `${target} updater artifact ${filename}`,
    );
    const entry = entries.get(filename);
    const size = statSync(artifactPath).size;
    const digest = sha512(artifactPath);
    if (!Number.isSafeInteger(entry.size) || entry.size !== size) {
      throw new Error(
        `[update-metadata] ${filename} size ${String(entry.size)} does not match ${size}`,
      );
    }
    if (entry.sha512 !== digest) {
      throw new Error(`[update-metadata] ${filename} SHA-512 does not match final bytes`);
    }
  }
  const primaryEntry = entries.get(expected.primary);
  if (metadata.path !== expected.primary || metadata.sha512 !== primaryEntry.sha512) {
    throw new Error(
      `[update-metadata] ${expected.metadata} primary path/hash must match ${expected.primary}`,
    );
  }
  const blockmapRoot = mkdtempSync(
    path.join(os.tmpdir(), "echodesk-update-blockmap-verify-"),
  );
  try {
    for (const filename of expected.blockmaps) {
      const blockmap = requiredFile(
        path.join(releaseRoot, filename),
        `${target} updater blockmap ${filename}`,
      );
      if (statSync(blockmap).size === 0) {
        throw new Error(`[update-metadata] empty updater blockmap: ${filename}`);
      }
      const artifactName = filename.slice(0, -".blockmap".length);
      const artifactPath = requiredFile(
        path.join(releaseRoot, artifactName),
        `${target} updater artifact for ${filename}`,
      );
      const recomputed = path.join(blockmapRoot, filename);
      await buildBlockMap(artifactPath, "gzip", recomputed);
      const actualBlockmap = readExternalBlockmap(blockmap, filename);
      const expectedBlockmap = readExternalBlockmap(recomputed, filename);
      if (!actualBlockmap.equals(expectedBlockmap)) {
        throw new Error(
          `[update-metadata] ${filename} does not match final artifact bytes`,
        );
      }
    }
    for (const filename of expected.embeddedBlockmaps || []) {
      const artifactPath = requiredFile(
        path.join(releaseRoot, filename),
        `${target} updater artifact for embedded blockmap ${filename}`,
      );
      await verifyEmbeddedBlockmap(
        artifactPath,
        entries.get(filename),
        blockmapRoot,
        filename,
      );
    }
  } finally {
    rmSync(blockmapRoot, { recursive: true, force: true });
  }

  console.log(
    `[update-metadata] verified ${target} ${version}: ${expected.files.join(", ")}`,
  );
  return { metadataPath, version, files: expected.files };
}

if (require.main === module) {
  verifyReleaseUpdateMetadata(process.argv[2]).catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}

module.exports = { verifyReleaseUpdateMetadata };

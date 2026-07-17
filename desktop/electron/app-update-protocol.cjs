"use strict";

const { createHash, randomBytes } = require("node:crypto");
const {
  chmodSync,
  createWriteStream,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { fetchBoundedHttpsJson } = require("./bounded-https-json.cjs");

const MAX_RELEASES_BYTES = 4 * 1024 * 1024;
const MAX_UPDATE_BYTES = 1024 * 1024 * 1024;
const ALLOWED_DOWNLOAD_HOSTS = new Set(["github.com", "api.github.com"]);

class AppUpdateProtocolError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = "AppUpdateProtocolError";
    this.code = code;
  }
}

function parseSemver(raw) {
  const value = String(raw || "").trim().replace(/^v/i, "");
  const match = value.match(
    /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$/,
  );
  if (!match) throw new AppUpdateProtocolError("UPDATE_VERSION_INVALID");
  return {
    raw: value,
    core: [Number(match[1]), Number(match[2]), Number(match[3])],
    prerelease: match[4] ? match[4].split(".") : null,
  };
}

function compareIdentifiers(a, b) {
  const numericA = /^\d+$/.test(a);
  const numericB = /^\d+$/.test(b);
  if (numericA && numericB) {
    const aa = BigInt(a);
    const bb = BigInt(b);
    return aa === bb ? 0 : aa > bb ? 1 : -1;
  }
  if (numericA !== numericB) return numericA ? -1 : 1;
  return a === b ? 0 : a > b ? 1 : -1;
}

function compareSemver(left, right) {
  const a = parseSemver(left);
  const b = parseSemver(right);
  for (let index = 0; index < 3; index += 1) {
    if (a.core[index] !== b.core[index]) {
      return a.core[index] > b.core[index] ? 1 : -1;
    }
  }
  if (a.prerelease === null || b.prerelease === null) {
    return a.prerelease === b.prerelease ? 0 : a.prerelease === null ? 1 : -1;
  }
  for (
    let index = 0;
    index < Math.max(a.prerelease.length, b.prerelease.length);
    index += 1
  ) {
    const aa = a.prerelease[index];
    const bb = b.prerelease[index];
    if (aa === undefined || bb === undefined) {
      return aa === bb ? 0 : aa === undefined ? -1 : 1;
    }
    const order = compareIdentifiers(aa, bb);
    if (order !== 0) return order;
  }
  return 0;
}

function updateAssetName(platform, version) {
  if (platform === "darwin") return `EchoDesk-${version}-arm64-mac.zip`;
  if (platform === "win32") return `EchoDesk.Setup.${version}.exe`;
  if (platform === "android") {
    return `EchoDesk-${version}-android-universal-PREVIEW.apk`;
  }
  return null;
}

function normalizeDigest(raw) {
  const match = String(raw || "").trim().match(/^sha256:([0-9a-f]{64})$/i);
  if (!match) return null;
  return match[1].toLowerCase();
}

function releaseVersionForChannel(release, channel) {
  if (!release || release.draft === true) return null;
  if (channel === "preview") {
    const tag = String(release.tag_name || "").trim();
    if (
      release.prerelease !== true ||
      !/^v\d+\.\d+\.\d+-preview\.\d+$/.test(tag)
    ) {
      return null;
    }
    return parseSemver(tag).raw;
  }
  if (channel !== "stable" || release.prerelease === true) return null;
  try {
    return parseSemver(release.tag_name || release.name).raw;
  } catch {
    return null;
  }
}

function isCompatibleUpgrade(version, currentVersion, channel) {
  if (channel !== "preview") {
    return compareSemver(version, currentVersion) > 0;
  }
  const next = String(version).match(/^(\d+)\.(\d+)\.(\d+)-preview\.(\d+)$/);
  const current = String(currentVersion).match(
    /^(\d+)\.(\d+)\.(\d+)-preview\.(\d+)$/,
  );
  if (!next || !current) return false;
  return (
    next[1] === current[1] &&
    next[2] === current[2] &&
    next[3] === current[3] &&
    BigInt(next[4]) > BigInt(current[4])
  );
}

function selectRelease(releases, { currentVersion, channel, platform }) {
  const candidates = [];
  for (const release of Array.isArray(releases) ? releases : []) {
    const version = releaseVersionForChannel(release, channel);
    if (!version || !isCompatibleUpgrade(version, currentVersion, channel)) continue;
    const expectedName = updateAssetName(platform, version);
    if (!expectedName) continue;
    const matches = (Array.isArray(release.assets) ? release.assets : []).filter(
      (asset) => asset?.name === expectedName,
    );
    if (matches.length !== 1) continue;
    const asset = matches[0];
    const digest = normalizeDigest(asset.digest);
    if (
      !digest ||
      !Number.isSafeInteger(asset.size) ||
      asset.size < 1 ||
      asset.size > MAX_UPDATE_BYTES ||
      typeof asset.browser_download_url !== "string"
    ) {
      continue;
    }
    candidates.push({
      version,
      releaseName: release.name || release.tag_name || "",
      releaseUrl: release.html_url,
      asset: {
        name: expectedName,
        url: asset.browser_download_url,
        size: asset.size,
        digest: `sha256:${digest}`,
      },
    });
  }
  candidates.sort((a, b) => compareSemver(b.version, a.version));
  return candidates[0] || null;
}

function allowedDownloadUrl(raw) {
  let target;
  try {
    target = new URL(String(raw || ""));
  } catch {
    throw new AppUpdateProtocolError("UPDATE_DOWNLOAD_URL_INVALID");
  }
  const host = target.hostname.toLowerCase();
  if (
    target.protocol !== "https:" ||
    target.username ||
    target.password ||
    (!ALLOWED_DOWNLOAD_HOSTS.has(host) &&
      !host.endsWith(".githubusercontent.com"))
  ) {
    throw new AppUpdateProtocolError("UPDATE_DOWNLOAD_URL_INVALID");
  }
  return target;
}

function downloadVerifiedAsset(
  asset,
  destination,
  { onProgress = () => {}, getImpl = https.get, redirects = 0 } = {},
) {
  const target = allowedDownloadUrl(asset.url);
  const expectedDigest = normalizeDigest(asset.digest);
  if (!expectedDigest) {
    return Promise.reject(
      new AppUpdateProtocolError("UPDATE_DIGEST_REQUIRED"),
    );
  }
  if (redirects > 5) {
    return Promise.reject(
      new AppUpdateProtocolError("UPDATE_REDIRECT_LIMIT"),
    );
  }
  return new Promise((resolve, reject) => {
    const request = getImpl(
      target,
      {
        headers: {
          Accept: "application/octet-stream",
          "User-Agent": "EchoDesk-Updater",
        },
      },
      (response) => {
        const status = Number(response.statusCode || 0);
        if (status >= 300 && status < 400 && response.headers?.location) {
          response.resume?.();
          let redirected;
          try {
            redirected = new URL(response.headers.location, target);
            allowedDownloadUrl(redirected);
          } catch (error) {
            reject(error);
            return;
          }
          downloadVerifiedAsset(
            { ...asset, url: redirected.toString() },
            destination,
            { onProgress, getImpl, redirects: redirects + 1 },
          ).then(resolve, reject);
          return;
        }
        if (status < 200 || status >= 300) {
          response.resume?.();
          reject(new AppUpdateProtocolError("UPDATE_DOWNLOAD_HTTP_ERROR"));
          return;
        }
        const declared = Number(response.headers?.["content-length"] || 0);
        if (
          (declared && declared !== asset.size) ||
          asset.size > MAX_UPDATE_BYTES
        ) {
          response.resume?.();
          reject(new AppUpdateProtocolError("UPDATE_DOWNLOAD_SIZE_MISMATCH"));
          return;
        }
        const hash = createHash("sha256");
        const output = createWriteStream(destination, {
          flags: "wx",
          mode: 0o600,
        });
        let received = 0;
        let settled = false;
        const fail = (error) => {
          if (settled) return;
          settled = true;
          output.destroy();
          rmSync(destination, { force: true });
          reject(error);
        };
        output.on("error", fail);
        response.on("error", fail);
        response.on("data", (chunk) => {
          if (settled) return;
          received += chunk.length;
          if (received > asset.size || received > MAX_UPDATE_BYTES) {
            response.destroy();
            fail(new AppUpdateProtocolError("UPDATE_DOWNLOAD_SIZE_MISMATCH"));
            return;
          }
          hash.update(chunk);
          if (!output.write(chunk)) response.pause();
          onProgress(Math.floor((received / asset.size) * 100));
        });
        output.on("drain", () => response.resume());
        response.on("end", () => {
          if (settled) return;
          output.end(() => {
            if (
              received !== asset.size ||
              hash.digest("hex") !== expectedDigest
            ) {
              fail(new AppUpdateProtocolError("UPDATE_DIGEST_MISMATCH"));
              return;
            }
            settled = true;
            chmodSync(destination, 0o600);
            resolve(destination);
          });
        });
      },
    );
    request.on("error", (error) =>
      reject(
        error instanceof AppUpdateProtocolError
          ? error
          : new AppUpdateProtocolError("UPDATE_DOWNLOAD_NETWORK_ERROR"),
      ),
    );
  });
}

function isReleaseList(payload) {
  return (
    Array.isArray(payload) &&
    payload.length <= 100 &&
    payload.every(
      (release) =>
        release &&
        typeof release === "object" &&
        Array.isArray(release.assets),
    )
  );
}

function createAppUpdateManager({
  owner,
  repo,
  currentVersion,
  platform,
  channel = "preview",
  tempRoot,
  helperPath,
  executablePath,
  currentPid,
  currentBundlePath = null,
  emit,
  quit,
  fetchReleases = null,
  spawnImpl = spawn,
}) {
  const releasesUrl = `https://github.com/${owner}/${repo}/releases`;
  const apiUrl =
    `https://api.github.com/repos/${owner}/${repo}/releases?per_page=20`;
  let selected = null;
  let downloaded = null;

  const status = (payload) => emit({ releaseUrl: releasesUrl, ...payload });

  async function check() {
    status({ status: "checking" });
    const releases = fetchReleases
      ? await fetchReleases(apiUrl)
      : await fetchBoundedHttpsJson(apiUrl, {
          headers: {
            Accept: "application/vnd.github+json",
            "User-Agent": `EchoDesk/${currentVersion}`,
          },
          maxBytes: MAX_RELEASES_BYTES,
          timeoutMs: 8_000,
          validate: isReleaseList,
        });
    selected = selectRelease(releases, {
      currentVersion,
      channel,
      platform,
    });
    const result = selected
      ? {
          status: "available",
          currentVersion,
          latestVersion: selected.version,
          updateAvailable: true,
          releaseName: selected.releaseName,
          releaseUrl: selected.releaseUrl || releasesUrl,
          assetName: selected.asset.name,
          assetUrl: selected.asset.url,
          assetDigest: selected.asset.digest,
          assetSize: selected.asset.size,
          canAutoInstall: platform === "darwin" || platform === "win32",
          requiresUserConfirmation: platform === "android",
        }
      : {
          status: "current",
          currentVersion,
          latestVersion: currentVersion,
          updateAvailable: false,
          releaseUrl: releasesUrl,
          assetName: null,
          assetUrl: null,
          canAutoInstall: false,
        };
    status(result);
    return result;
  }

  async function download() {
    if (!selected) await check();
    if (!selected) {
      throw new AppUpdateProtocolError("UPDATE_NOT_AVAILABLE");
    }
    mkdirSync(tempRoot, { recursive: true, mode: 0o700 });
    const owned = mkdtempSync(path.join(tempRoot, "task-"));
    const destination = path.join(owned, selected.asset.name);
    status({
      status: "downloading",
      percent: 0,
      latestVersion: selected.version,
      updateAvailable: true,
      canAutoInstall: platform === "darwin" || platform === "win32",
    });
    await downloadVerifiedAsset(selected.asset, destination, {
      onProgress: (percent) =>
        status({ status: "downloading", percent, updateAvailable: true }),
    });
    downloaded = { directory: owned, path: destination, release: selected };
    status({
      status: "downloaded",
      percent: 100,
      latestVersion: selected.version,
      updateAvailable: true,
      assetName: selected.asset.name,
      assetDigest: selected.asset.digest,
      assetSize: selected.asset.size,
      canAutoInstall: platform === "darwin" || platform === "win32",
      requiresUserConfirmation: platform === "android",
    });
    return downloaded;
  }

  async function install() {
    if (platform !== "darwin" && platform !== "win32") {
      throw new AppUpdateProtocolError("UPDATE_PLATFORM_INSTALL_UNSUPPORTED");
    }
    if (!downloaded) await download();
    const planPath = path.join(
      downloaded.directory,
      `install-${randomBytes(8).toString("hex")}.json`,
    );
    const plan = {
      schema: 1,
      platform,
      parentPid: currentPid,
      artifactPath: downloaded.path,
      expectedSha256: normalizeDigest(downloaded.release.asset.digest),
      expectedSize: downloaded.release.asset.size,
      executablePath,
      currentBundlePath,
      backupPath:
        platform === "darwin"
          ? `${currentBundlePath}.update-backup`
          : null,
    };
    writeFileSync(planPath, `${JSON.stringify(plan)}\n`, {
      mode: 0o600,
      flag: "wx",
    });
    const child = spawnImpl(executablePath, [helperPath, planPath], {
      detached: true,
      stdio: "ignore",
      env: {
        ...process.env,
        ELECTRON_RUN_AS_NODE: "1",
      },
    });
    child.unref();
    status({
      status: "installing",
      latestVersion: downloaded.release.version,
      updateAvailable: true,
      canAutoInstall: true,
    });
    quit();
    return { ok: true };
  }

  return { check, download, install };
}

module.exports = {
  AppUpdateProtocolError,
  compareSemver,
  createAppUpdateManager,
  downloadVerifiedAsset,
  isCompatibleUpgrade,
  normalizeDigest,
  parseSemver,
  releaseVersionForChannel,
  selectRelease,
  updateAssetName,
};

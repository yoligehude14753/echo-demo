"use strict";

const { execFileSync } = require("node:child_process");
const { writeFileSync } = require("node:fs");

const {
  OWNER,
  REPO,
  TARGET_VERSION,
  assertVersionContract,
  canonicalAssets,
} = require("./preview-update-contract.cjs");

function parseArgs(argv) {
  const options = { output: null, expectedSha: null };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    const value = argv[index + 1];
    if (name === "--expected-sha" && value) {
      options.expectedSha = value;
      index += 1;
    } else if (name === "--output" && value) {
      options.output = value;
      index += 1;
    } else {
      throw new Error(`unknown or incomplete argument: ${name}`);
    }
  }
  if (!/^[0-9a-f]{40}$/.test(options.expectedSha || "")) {
    throw new Error("--expected-sha must be a lowercase 40-character Git SHA");
  }
  return options;
}

function ghJson(endpoint, gh = execFileSync) {
  return JSON.parse(
    gh("gh", ["api", endpoint], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }),
  );
}

function resolveTagSha(tag, gh = execFileSync) {
  const ref = ghJson(`repos/${OWNER}/${REPO}/git/ref/tags/${tag}`, gh);
  if (ref.object?.type === "commit") return ref.object.sha;
  if (ref.object?.type !== "tag" || !/^[0-9a-f]{40}$/.test(ref.object?.sha || "")) {
    throw new Error("release tag ref does not resolve to a commit or annotated tag");
  }
  const annotated = ghJson(`repos/${OWNER}/${REPO}/git/tags/${ref.object.sha}`, gh);
  if (annotated.object?.type !== "commit") {
    throw new Error("annotated release tag does not point directly to a commit");
  }
  return annotated.object.sha;
}

function validateRelease(release, expectedSha, tagSha) {
  const tag = `v${TARGET_VERSION}`;
  if (tagSha !== expectedSha) {
    throw new Error(`release tag points to ${tagSha}, expected ${expectedSha}`);
  }
  if (
    release?.tag_name !== tag ||
    release?.draft !== false ||
    release?.prerelease !== false ||
    release?.name !== `EchoDesk ${TARGET_VERSION}` ||
    typeof release?.body !== "string" ||
    !release.body.includes(TARGET_VERSION) ||
    typeof release?.html_url !== "string"
  ) {
    throw new Error("release must be the public 0.3.4 stable release with versioned notes");
  }
  const expectedNames = Object.values(canonicalAssets()).sort();
  const actualNames = (release.assets || []).map((asset) => asset?.name).sort();
  if (JSON.stringify(actualNames) !== JSON.stringify(expectedNames)) {
    throw new Error(
      `release assets ${JSON.stringify(actualNames)} != ${JSON.stringify(expectedNames)}`,
    );
  }
  const assets = {};
  for (const expectedName of expectedNames) {
    const matches = release.assets.filter((asset) => asset?.name === expectedName);
    const asset = matches[0];
    if (
      matches.length !== 1 ||
      !Number.isSafeInteger(asset.size) ||
      asset.size < 1 ||
      !/^sha256:[0-9a-f]{64}$/i.test(asset.digest || "") ||
      typeof asset.browser_download_url !== "string"
    ) {
      throw new Error(`release asset ${expectedName} lacks unique size/digest/download evidence`);
    }
    assets[expectedName] = {
      id: asset.id,
      size: asset.size,
      digest: asset.digest.toLowerCase(),
      browserDownloadUrl: asset.browser_download_url,
    };
  }
  return {
    schema: 1,
    repository: `${OWNER}/${REPO}`,
    sourceSha: expectedSha,
    tag,
    releaseId: release.id,
    releaseUrl: release.html_url,
    prerelease: false,
    releaseName: release.name,
    releaseNotes: release.body,
    assets,
  };
}

function main(argv = process.argv.slice(2), gh = execFileSync) {
  const options = parseArgs(argv);
  assertVersionContract();
  const tag = `v${TARGET_VERSION}`;
  const release = ghJson(`repos/${OWNER}/${REPO}/releases/tags/${tag}`, gh);
  const evidence = validateRelease(
    release,
    options.expectedSha,
    resolveTagSha(tag, gh),
  );
  const serialized = `${JSON.stringify(evidence, null, 2)}\n`;
  if (options.output) {
    writeFileSync(options.output, serialized, { mode: 0o600, flag: "wx" });
  }
  process.stdout.write(serialized);
  return evidence;
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`[preview-update-release-readback] ${error?.message || error}\n`);
    process.exitCode = 1;
  }
}

module.exports = { main, parseArgs, resolveTagSha, validateRelease };

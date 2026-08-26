const patternSource = require("./release-asset-patterns.json");

function preferredReleaseAsset(assets, platform = process.platform) {
  const candidates = Array.isArray(assets) ? assets : [];
  const patterns = (patternSource[platform] || []).map(
    (source) => new RegExp(source, "i"),
  );

  for (const pattern of patterns) {
    const asset = candidates.find((candidate) =>
      pattern.test(String(candidate?.name || "")),
    );
    if (asset) return asset;
  }

  // Never fall back to an arbitrary asset: a release may intentionally carry
  // only Android/TV packages while desktop signing is still pending.
  return null;
}

module.exports = { preferredReleaseAsset };

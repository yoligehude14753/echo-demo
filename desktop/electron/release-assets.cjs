function preferredReleaseAsset(assets, platform = process.platform) {
  const candidates = Array.isArray(assets) ? assets : [];
  const patterns =
    platform === "darwin"
      ? [/arm64\.dmg$/i, /arm64-mac\.zip$/i, /\.dmg$/i]
      : platform === "win32"
        ? [/Setup\.[\d.]+\.exe$/i, /\.exe$/i]
        : platform === "linux"
          ? [/\.AppImage$/i, /\.deb$/i]
          : [];

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

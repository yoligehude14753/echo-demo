"use strict";

const AUDIO_PERMISSIONS = new Set(["media", "microphone", "audioCapture"]);

function installMediaPermissionHandlers(
  targetSession,
  { isTrustedRendererUrl, isTrustedRendererOrigin },
) {
  if (
    !targetSession ||
    typeof targetSession.setPermissionRequestHandler !== "function" ||
    typeof targetSession.setPermissionCheckHandler !== "function" ||
    typeof isTrustedRendererUrl !== "function" ||
    typeof isTrustedRendererOrigin !== "function"
  ) {
    throw new TypeError("media permission policy dependencies are required");
  }

  const trustedWebContents = (webContents) => {
    try {
      return isTrustedRendererUrl(webContents?.getURL?.() || "");
    } catch {
      return false;
    }
  };
  const audioOnly = (permission, details) => {
    if (!AUDIO_PERMISSIONS.has(permission)) return false;
    if (permission !== "media") return true;
    if (Array.isArray(details?.mediaTypes)) {
      return details.mediaTypes.length === 1 && details.mediaTypes[0] === "audio";
    }
    return details?.mediaType === "audio";
  };
  const trustedMainFrame = (webContents, permission, details) => {
    if (!trustedWebContents(webContents) || details?.isMainFrame !== true) return false;
    if (!audioOnly(permission, details)) return false;
    if (
      typeof details.requestingUrl !== "string" ||
      !isTrustedRendererUrl(details.requestingUrl)
    ) {
      return false;
    }
    if (
      details.securityOrigin !== undefined &&
      !isTrustedRendererOrigin(details.securityOrigin)
    ) {
      return false;
    }
    if (
      details.embeddingOrigin !== undefined &&
      !isTrustedRendererOrigin(details.embeddingOrigin)
    ) {
      return false;
    }
    return true;
  };

  targetSession.setPermissionRequestHandler(
    (webContents, permission, callback, details) => {
      callback(trustedMainFrame(webContents, permission, details));
    },
  );

  targetSession.setPermissionCheckHandler(
    (webContents, permission, requestingOrigin, details) =>
      isTrustedRendererOrigin(requestingOrigin) &&
      trustedMainFrame(webContents, permission, details),
  );
}

module.exports = { installMediaPermissionHandlers };

"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  installMediaPermissionHandlers,
} = require("../media-permission-policy.cjs");

const APP_URL = "echodesk://app/index.html";
const APP_ORIGIN = "echodesk://app";

function installedPolicy() {
  let requestHandler = null;
  let checkHandler = null;
  installMediaPermissionHandlers(
    {
      setPermissionRequestHandler(handler) {
        requestHandler = handler;
      },
      setPermissionCheckHandler(handler) {
        checkHandler = handler;
      },
    },
    {
      isTrustedRendererUrl: (value) => value === APP_URL,
      isTrustedRendererOrigin: (value) => value === APP_ORIGIN,
    },
  );
  return { requestHandler, checkHandler };
}

const mainContents = { getURL: () => APP_URL };

test("permission request handler allows only trusted main-frame audio", () => {
  const { requestHandler } = installedPolicy();
  const request = (details, permission = "media", webContents = mainContents) => {
    let allowed = null;
    requestHandler(webContents, permission, (value) => {
      allowed = value;
    }, details);
    return allowed;
  };

  assert.equal(
    request({
      isMainFrame: true,
      requestingUrl: APP_URL,
      securityOrigin: APP_ORIGIN,
      mediaTypes: ["audio"],
    }),
    true,
  );
  assert.equal(
    request({ isMainFrame: false, requestingUrl: "https://backend.example/frame" }),
    false,
  );
  assert.equal(
    request({ isMainFrame: true, requestingUrl: "https://backend.example/frame" }),
    false,
  );
  assert.equal(
    request({ isMainFrame: true, requestingUrl: APP_URL, mediaTypes: ["video"] }),
    false,
  );
  assert.equal(
    request({
      isMainFrame: true,
      requestingUrl: APP_URL,
      mediaTypes: ["audio", "video"],
    }),
    false,
    "a combined camera+microphone request must not inherit microphone trust",
  );
  assert.equal(
    request({ isMainFrame: true, requestingUrl: APP_URL, mediaTypes: [] }),
    false,
  );
  assert.equal(
    request({ isMainFrame: true, requestingUrl: APP_URL }, "geolocation"),
    false,
  );
});

test("permission check handler binds requesting origin and frame details", () => {
  const { checkHandler } = installedPolicy();
  const trustedDetails = {
    isMainFrame: true,
    requestingUrl: APP_URL,
    securityOrigin: APP_ORIGIN,
    mediaType: "audio",
  };

  assert.equal(checkHandler(mainContents, "media", APP_ORIGIN, trustedDetails), true);
  assert.equal(
    checkHandler(mainContents, "media", "https://backend.example", trustedDetails),
    false,
  );
  assert.equal(
    checkHandler(mainContents, "media", APP_ORIGIN, {
      ...trustedDetails,
      isMainFrame: false,
      requestingUrl: undefined,
      embeddingOrigin: APP_ORIGIN,
    }),
    false,
  );
  assert.equal(
    checkHandler(mainContents, "media", APP_ORIGIN, {
      ...trustedDetails,
      requestingUrl: "https://artifact.example/preview",
    }),
    false,
  );
});

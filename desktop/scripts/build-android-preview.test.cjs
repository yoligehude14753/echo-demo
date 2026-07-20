const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");

const ROOT = join(__dirname, "..");
const builder = readFileSync(join(__dirname, "build-android-preview.cjs"), "utf8");
const gradle = readFileSync(join(ROOT, "android", "app", "build.gradle"), "utf8");
const runtime = readFileSync(join(ROOT, "src", "runtime.ts"), "utf8");
const onboarding = readFileSync(
  join(ROOT, "src", "components", "OnboardingModal.tsx"),
  "utf8",
);
const backendConfig = JSON.parse(
  readFileSync(join(ROOT, "backend.config.json"), "utf8"),
);

test("Android Preview builder pins artifact and in-APK preview version", () => {
  assert.match(builder, /PREVIEW_VERSION = "0\.3\.4"/);
  assert.match(builder, /PREVIEW_VERSION_CODE = "30401"/);
  assert.match(
    builder,
    /EchoDesk-\$\{PREVIEW_VERSION\}-android-universal\.apk/,
  );
  assert.match(builder, /-PechoPreviewVersionName=\$\{PREVIEW_VERSION\}/);
  assert.match(gradle, /versionName previewSigningRequested/);
});

test("Android Preview builder is explicitly remote-mobile release public", () => {
  assert.match(builder, /VITE_ECHODESK_RUNTIME_MODE: "release"/);
  assert.match(builder, /VITE_ECHODESK_PRINCIPAL_MODE: "public"/);
  assert.doesNotMatch(builder, /VITE_ECHODESK_RUNTIME_MODE: "development"/);
  assert.doesNotMatch(builder, /VITE_ECHODESK_PRINCIPAL_MODE: "local"/);
});

test("Android Preview release runtime and onboarding pin the public endpoint", () => {
  assert.equal(
    backendConfig.roles.publicService.baseUrl,
    "https://echodesk.yoliyoli.uk",
  );
  assert.match(
    runtime,
    /if \(runtimeMode\(\) === "release"\) return DEFAULT_ANDROID_BACKEND_BASE;/,
  );
  assert.match(onboarding, /本 Preview 构建固定连接该公共服务/);
  assert.match(onboarding, /不能在设置中改写业务 endpoint/);
});

test("Android Preview uses a non-debuggable release variant", () => {
  assert.match(builder, /"assembleRelease"/);
  assert.doesNotMatch(builder, /assembleDebug/);
  assert.match(builder, /Preview APK must not be debuggable/);
  assert.match(builder, /\^application-debuggable\$/m);
  assert.match(builder, /outputs[\s\S]*apk[\s\S]*release[\s\S]*app-release\.apk/);
});

test("Android Preview signing uses a stable external CI/env identity", () => {
  assert.match(builder, /ECHODESK_ANDROID_PREVIEW_EXPECTED_CERT_SHA256/);
  assert.match(builder, /stable Preview keystore must remain outside the repository/);
  assert.match(builder, /preview\.2 random-signed installs require one uninstall/);
  assert.doesNotMatch(builder, /randomBytes|genkeypair|\.preview-signing/);
  assert.match(gradle, /echoPreviewSigning/);
  assert.match(gradle, /ECHODESK_ANDROID_PREVIEW_KEYSTORE/);
  assert.match(gradle, /signingConfig signingConfigs\.preview/);
});

test("official release external-signing gate remains fail-closed", () => {
  assert.match(
    gradle,
    /if \(releaseTaskRequested && !previewSigningRequested\) \{[\s\S]*if \(!externalSigningRequested\)/,
  );
  assert.match(
    gradle,
    /Public Android release must use the controlled v3\.1 external signing pipeline/,
  );
  assert.match(gradle, /Public Android release requires legacy\/current signing inputs/);
});

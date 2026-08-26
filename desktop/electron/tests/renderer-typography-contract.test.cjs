"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");

const DESKTOP_ROOT = resolve(__dirname, "../..");
const rendererCss = readFileSync(resolve(DESKTOP_ROOT, "src/index.css"), "utf8");
const rendererEntry = readFileSync(resolve(DESKTOP_ROOT, "src/main.tsx"), "utf8");
const tailwindConfig = readFileSync(resolve(DESKTOP_ROOT, "tailwind.config.js"), "utf8");
const bootHtml = readFileSync(resolve(DESKTOP_ROOT, "index.html"), "utf8");
const docxPreview = readFileSync(
  resolve(DESKTOP_ROOT, "src/components/ArtifactPreviewModal.tsx"),
  "utf8",
);
const allTypographySources = [
  rendererCss,
  rendererEntry,
  tailwindConfig,
  bootHtml,
  docxPreview,
].join("\n");

test("renderer typography has one primary family across every entry point", () => {
  assert.match(
    rendererCss,
    /--ed-font-primary:\s*"Noto Sans CJK SC"[\s\S]*?--ed-font-ui:\s*var\(--ed-font-primary\)/,
  );
  assert.match(rendererCss, /--ed-font-reading:\s*var\(--ed-font-primary\)/);
  assert.match(rendererCss, /--ed-font-mono:\s*var\(--ed-font-primary\)/);
  assert.match(rendererEntry, /fontFamily:\s*"var\(--ed-font-ui\)"/);
  assert.match(tailwindConfig, /serif:\s*\["var\(--ed-font-primary\)"\]/);
  assert.match(tailwindConfig, /mono:\s*\["var\(--ed-font-primary\)"\]/);
  assert.match(bootHtml, /font-family:\s*'Noto Sans CJK SC'/);
  assert.match(docxPreview, /font-family: "Noto Sans CJK SC"/);
  assert.doesNotMatch(
    allTypographySources,
    /Charter|New York|Songti|FangSong|STFangsong|Avenir Next|SF Mono|Roboto Mono/,
  );
});

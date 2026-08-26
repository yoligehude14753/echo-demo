import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node strip-types requires the explicit source extension.
import { artifactDownloadName } from "./artifactDownloadName.ts";

test("adds the canonical extension for generated artifacts", () => {
  assert.equal(
    artifactDownloadName({ artifact_type: "pptx", title: "发布计划" }),
    "发布计划.pptx",
  );
  assert.equal(
    artifactDownloadName({ artifact_type: "word", title: "会议纪要" }),
    "会议纪要.docx",
  );
  assert.equal(
    artifactDownloadName({ artifact_type: "markdown", title: "说明" }),
    "说明.md",
  );
});

test("does not duplicate an existing canonical extension", () => {
  assert.equal(
    artifactDownloadName({ artifact_type: "pptx", title: "发布计划.PPTX" }),
    "发布计划.PPTX",
  );
});

test("uses a safe basename and preserves the canonical extension", () => {
  assert.equal(
    artifactDownloadName({
      artifact_type: "pptx",
      title: "../unsafe/path/发布:计划?. ",
    }),
    "发布-计划-.pptx",
  );
  const longName = artifactDownloadName({
    artifact_type: "html",
    title: "a".repeat(240),
  });
  assert.equal(longName.length, 160);
  assert.ok(longName.endsWith(".html"));
});

test("falls back to a typed filename when title is blank", () => {
  assert.equal(
    artifactDownloadName({ artifact_type: "xlsx", title: "   " }),
    "echodesk-artifact.xlsx",
  );
});

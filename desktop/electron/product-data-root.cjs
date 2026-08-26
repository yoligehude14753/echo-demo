"use strict";

const path = require("node:path");

const LEGACY_CANDIDATE_PRODUCT_NAME = "EchoDesk Legacy Candidate";

function resolveProductDataRoot({ env = process.env, homeDir, productName }) {
  const explicit = String(env.ECHO_USER_DIR || "").trim();
  if (explicit) {
    if (!path.isAbsolute(explicit)) {
      throw new Error("ECHO_USER_DIR must be absolute");
    }
    return path.resolve(explicit);
  }

  const directoryName = productName === LEGACY_CANDIDATE_PRODUCT_NAME
    ? ".echodesk-legacy-candidate"
    : ".echodesk";
  return path.join(path.resolve(homeDir), directoryName);
}

module.exports = {
  LEGACY_CANDIDATE_PRODUCT_NAME,
  resolveProductDataRoot,
};

"use strict";

function electronNodeRuntimeEnvironment(electronExecutable) {
  const executable = String(electronExecutable || "").trim();
  if (!executable) {
    throw new TypeError(
      "Electron executable is required for the backend Node runtime",
    );
  }
  return {
    ECHODESK_NODE_RUNTIME: executable,
    ECHODESK_NODE_RUNTIME_IS_ELECTRON: "1",
  };
}

module.exports = { electronNodeRuntimeEnvironment };

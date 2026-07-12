/* eslint-disable @typescript-eslint/no-var-requires, no-undef */
const {
  existsSync,
  openSync,
  closeSync,
  readSync,
  rmSync,
  statSync,
} = require("node:fs");
const { join, resolve } = require("node:path");
const { spawnSync } = require("node:child_process");
const { verifyFrozenAnalysis } = require("./backend-frozen-contract.cjs");

function peMachine(filePath) {
  const descriptor = openSync(filePath, "r");
  try {
    const header = Buffer.alloc(64);
    if (readSync(descriptor, header, 0, header.length, 0) !== header.length) {
      throw new Error("PE header is truncated");
    }
    if (header.readUInt16LE(0) !== 0x5a4d) {
      throw new Error("missing DOS MZ signature");
    }
    const peOffset = header.readUInt32LE(0x3c);
    const signature = Buffer.alloc(6);
    if (readSync(descriptor, signature, 0, signature.length, peOffset) !== signature.length) {
      throw new Error("PE signature is truncated");
    }
    if (signature.readUInt32LE(0) !== 0x00004550) {
      throw new Error("missing PE signature");
    }
    return signature.readUInt16LE(4);
  } finally {
    closeSync(descriptor);
  }
}

function validatePython(candidate, backendRoot) {
  const result = spawnSync(
    candidate,
    [
      "-c",
      "import platform, PyInstaller; print(platform.machine(), PyInstaller.__version__)",
    ],
    { cwd: backendRoot, encoding: "utf8", timeout: 10_000 },
  );
  if (result.error || result.status !== 0) return null;
  const output = `${result.stdout || ""}${result.stderr || ""}`.trim();
  if (!/^(amd64|x86_64)\s+/i.test(output)) {
    throw new Error(
      `[backend-build] refusing non-x64 Python ${candidate}: ${output}`,
    );
  }
  return output;
}

function main() {
  if (process.platform !== "win32" || process.arch !== "x64") {
    throw new Error("[backend-build] Windows backend requires a Windows x64 runner");
  }

  const desktopRoot = resolve(__dirname, "..");
  const repoRoot = resolve(desktopRoot, "..");
  const backendRoot = join(repoRoot, "backend");
  const outputPath = join(backendRoot, "dist", "echodesk-backend.exe");
  const specPath = join(backendRoot, "packaging", "echodesk-backend.spec");
  if (!existsSync(specPath)) {
    throw new Error(`[backend-build] missing PyInstaller spec: ${specPath}`);
  }

  const configuredPython = process.env.ECHODESK_BACKEND_PYTHON?.trim();
  const candidates = [
    configuredPython,
    join(backendRoot, ".venv", "Scripts", "python.exe"),
    "python",
    "py",
  ].filter(Boolean);
  let python = null;
  let pythonDescription = null;
  for (const candidate of candidates) {
    if (candidate.includes("\\") && !existsSync(candidate)) continue;
    const description = validatePython(candidate, backendRoot);
    if (!description) continue;
    python = candidate;
    pythonDescription = description;
    break;
  }
  if (!python) {
    throw new Error(
      `[backend-build] no Windows x64 Python with PyInstaller found; searched: ${candidates.join(", ")}`,
    );
  }

  console.log(`[backend-build] python=${python} (${pythonDescription})`);
  rmSync(outputPath, { force: true });
  rmSync(join(backendRoot, "build", "echodesk-backend"), {
    recursive: true,
    force: true,
  });
  const build = spawnSync(
    python,
    [
      "-m",
      "PyInstaller",
      "--noconfirm",
      "--clean",
      "packaging/echodesk-backend.spec",
    ],
    { cwd: backendRoot, stdio: "inherit" },
  );
  if (build.error || build.status !== 0) {
    throw build.error || new Error(`[backend-build] PyInstaller exited with ${build.status}`);
  }
  verifyFrozenAnalysis(
    join(backendRoot, "build", "echodesk-backend", "Analysis-00.toc"),
  );
  if (!existsSync(outputPath) || !statSync(outputPath).isFile()) {
    throw new Error(`[backend-build] missing output: ${outputPath}`);
  }
  const machine = peMachine(outputPath);
  if (machine !== 0x8664) {
    throw new Error(
      `[backend-build] output is not an x64 PE executable: machine=0x${machine.toString(16)}`,
    );
  }
  console.log(`[backend-build] verified x64 PE executable: ${outputPath}`);
}

if (require.main === module) main();

module.exports = { peMachine };

/* eslint-disable @typescript-eslint/no-var-requires, no-undef */
const {
  constants,
  accessSync,
  chmodSync,
  existsSync,
  rmSync,
  statSync,
} = require("node:fs");
const { join, resolve } = require("node:path");
const { spawnSync } = require("node:child_process");
const { verifyFrozenAnalysis } = require("./backend-frozen-contract.cjs");

if (process.platform !== "linux" || process.arch !== "x64") {
  throw new Error("[backend-build] Linux backend requires an x64 Linux runner");
}

const desktopRoot = resolve(__dirname, "..");
const repoRoot = resolve(desktopRoot, "..");
const backendRoot = join(repoRoot, "backend");
const outputPath = join(backendRoot, "dist", "echodesk-backend");

function validatePython(candidate) {
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
  if (!/^(x86_64|amd64)\s+/i.test(output)) {
    throw new Error(
      `[backend-build] refusing non-x64 Python ${candidate}: ${output}`,
    );
  }
  return output;
}

const configuredPython = process.env.ECHODESK_BACKEND_PYTHON?.trim();
const candidates = [
  configuredPython,
  join(backendRoot, ".venv", "bin", "python"),
  "python3",
].filter(Boolean);
let python = null;
let pythonDescription = null;
for (const candidate of candidates) {
  if (candidate.includes("/") && !existsSync(candidate)) continue;
  const description = validatePython(candidate);
  if (!description) continue;
  python = candidate;
  pythonDescription = description;
  break;
}
if (!python) {
  throw new Error(
    `[backend-build] no x64 Python with PyInstaller found; searched: ${candidates.join(", ")}`,
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
  throw (
    build.error ||
    new Error(`[backend-build] PyInstaller exited with ${build.status}`)
  );
}
verifyFrozenAnalysis(
  join(backendRoot, "build", "echodesk-backend", "Analysis-00.toc"),
);
if (!existsSync(outputPath) || !statSync(outputPath).isFile()) {
  throw new Error(`[backend-build] missing output: ${outputPath}`);
}

chmodSync(outputPath, statSync(outputPath).mode | 0o111);
accessSync(outputPath, constants.X_OK);
const file = spawnSync("/usr/bin/file", [outputPath], { encoding: "utf8" });
const fileDescription = `${file.stdout || ""}${file.stderr || ""}`.trim();
if (
  file.error ||
  file.status !== 0 ||
  !/ELF 64-bit.*x86-64/i.test(fileDescription)
) {
  throw new Error(
    `[backend-build] output is not an x64 ELF executable: ${fileDescription}`,
  );
}
console.log(`[backend-build] verified ${fileDescription}`);

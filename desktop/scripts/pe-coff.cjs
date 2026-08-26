const { createHash } = require("node:crypto");
const {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} = require("node:fs");
const path = require("node:path");

function sha256File(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function isPeCoffBuffer(buffer) {
  if (!Buffer.isBuffer(buffer) || buffer.length < 64) return false;
  if (buffer[0] !== 0x4d || buffer[1] !== 0x5a) return false;
  const peOffset = buffer.readUInt32LE(0x3c);
  return peOffset >= 64
    && peOffset + 4 <= buffer.length
    && buffer.subarray(peOffset, peOffset + 4).equals(Buffer.from([0x50, 0x45, 0x00, 0x00]));
}

function isPeCoffFile(filePath) {
  try {
    return isPeCoffBuffer(readFileSync(filePath));
  } catch {
    return false;
  }
}

function walkRegularFiles(root, result = []) {
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true });
  } catch (error) {
    throw new Error(`[pe-coff] cannot enumerate ${root}: ${error instanceof Error ? error.message : String(error)}`);
  }
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    if (entry.isSymbolicLink()) continue;
    const candidate = path.join(root, entry.name);
    if (entry.isFile()) result.push(candidate);
    else if (entry.isDirectory()) walkRegularFiles(candidate, result);
  }
  return result;
}

function enumeratePeCoffFiles(root) {
  const resolvedRoot = path.resolve(root);
  if (!existsSync(resolvedRoot) || !statSync(resolvedRoot).isDirectory()) {
    throw new Error(`[pe-coff] root does not exist: ${resolvedRoot}`);
  }
  return walkRegularFiles(resolvedRoot)
    .filter((filePath) => isPeCoffFile(filePath))
    .map((filePath) => {
      const bytes = readFileSync(filePath);
      return {
        relative_path: path.relative(resolvedRoot, filePath).split(path.sep).join(path.posix.sep),
        absolute_path: filePath,
        size_bytes: bytes.length,
        sha256: sha256File(filePath),
      };
    })
    .sort((left, right) => left.relative_path.localeCompare(right.relative_path));
}

module.exports = { enumeratePeCoffFiles, isPeCoffBuffer, isPeCoffFile };

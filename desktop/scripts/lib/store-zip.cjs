"use strict";

const fs = require("node:fs");
const path = require("node:path");

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value & 1) === 1 ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
    }
    table[index] = value >>> 0;
  }
  return table;
})();

function crc32File(filePath) {
  const fd = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  let crc = 0xffffffff;
  try {
    for (;;) {
      const bytesRead = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      for (let index = 0; index < bytesRead; index += 1) {
        crc = CRC_TABLE[(crc ^ buffer[index]) & 0xff] ^ (crc >>> 8);
      }
    }
  } finally {
    fs.closeSync(fd);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function writeBuffer(fd, buffer) {
  let offset = 0;
  while (offset < buffer.length) {
    offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
  }
}

function copyFileToFd(sourcePath, outputFd) {
  const sourceFd = fs.openSync(sourcePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = fs.readSync(sourceFd, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      writeBuffer(outputFd, buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(sourceFd);
  }
}

function assertArchivePath(entryPath) {
  if (
    typeof entryPath !== "string"
    || entryPath.length === 0
    || entryPath.includes("\\")
    || entryPath.startsWith("/")
    || /^[A-Za-z]:/.test(entryPath)
  ) {
    throw new Error(`unsafe archive path: ${entryPath}`);
  }
  const normalized = path.posix.normalize(entryPath);
  if (normalized !== entryPath || normalized === ".." || normalized.startsWith("../")) {
    throw new Error(`unsafe archive path: ${entryPath}`);
  }
  return normalized;
}

function createStoreZip(entries, outputPath) {
  const normalizedEntries = entries.map((entry) => {
    const name = assertArchivePath(entry.name);
    const stat = fs.lstatSync(entry.source);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new Error(`zip payload must be a regular file: ${entry.source}`);
    }
    if (stat.size > 0xffffffff) {
      throw new Error(`ZIP64 is not supported: ${entry.source}`);
    }
    return {
      name,
      source: entry.source,
      size: stat.size,
      crc: crc32File(entry.source),
    };
  });

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const tempPath = `${outputPath}.tmp-${process.pid}`;
  const fd = fs.openSync(tempPath, "wx");
  const central = [];
  let archiveOffset = 0;
  try {
    for (const entry of normalizedEntries) {
      const nameBytes = Buffer.from(entry.name, "utf8");
      const localHeader = Buffer.alloc(30);
      localHeader.writeUInt32LE(0x04034b50, 0);
      localHeader.writeUInt16LE(20, 4);
      localHeader.writeUInt16LE(0x0800, 6);
      localHeader.writeUInt16LE(0, 8);
      localHeader.writeUInt32LE(0, 10);
      localHeader.writeUInt32LE(entry.crc, 14);
      localHeader.writeUInt32LE(entry.size, 18);
      localHeader.writeUInt32LE(entry.size, 22);
      localHeader.writeUInt16LE(nameBytes.length, 26);
      localHeader.writeUInt16LE(0, 28);
      writeBuffer(fd, localHeader);
      writeBuffer(fd, nameBytes);
      copyFileToFd(entry.source, fd);
      central.push({ ...entry, nameBytes, localOffset: archiveOffset });
      archiveOffset += localHeader.length + nameBytes.length + entry.size;
    }

    const centralOffset = archiveOffset;
    for (const entry of central) {
      const header = Buffer.alloc(46);
      header.writeUInt32LE(0x02014b50, 0);
      header.writeUInt16LE(20, 4);
      header.writeUInt16LE(20, 6);
      header.writeUInt16LE(0x0800, 8);
      header.writeUInt16LE(0, 10);
      header.writeUInt32LE(0, 12);
      header.writeUInt32LE(entry.crc, 16);
      header.writeUInt32LE(entry.size, 20);
      header.writeUInt32LE(entry.size, 24);
      header.writeUInt16LE(entry.nameBytes.length, 28);
      header.writeUInt16LE(0, 30);
      header.writeUInt16LE(0, 32);
      header.writeUInt16LE(0, 34);
      header.writeUInt16LE(0, 36);
      header.writeUInt32LE(0, 38);
      header.writeUInt32LE(entry.localOffset, 42);
      writeBuffer(fd, header);
      writeBuffer(fd, entry.nameBytes);
      archiveOffset += header.length + entry.nameBytes.length;
    }

    const centralSize = archiveOffset - centralOffset;
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(0, 4);
    eocd.writeUInt16LE(0, 6);
    eocd.writeUInt16LE(central.length, 8);
    eocd.writeUInt16LE(central.length, 10);
    eocd.writeUInt32LE(centralSize, 12);
    eocd.writeUInt32LE(centralOffset, 16);
    eocd.writeUInt16LE(0, 20);
    writeBuffer(fd, eocd);
    fs.fsyncSync(fd);
  } catch (error) {
    fs.closeSync(fd);
    fs.rmSync(tempPath, { force: true });
    throw error;
  }
  fs.closeSync(fd);
  fs.renameSync(tempPath, outputPath);
}

function readCentralDirectory(zipPath) {
  const fd = fs.openSync(zipPath, "r");
  try {
    const stat = fs.fstatSync(fd);
    const tailSize = Math.min(stat.size, 0xffff + 22);
    const tail = Buffer.alloc(tailSize);
    fs.readSync(fd, tail, 0, tailSize, stat.size - tailSize);
    let eocdOffset = -1;
    for (let index = tail.length - 22; index >= 0; index -= 1) {
      if (tail.readUInt32LE(index) === 0x06054b50) {
        eocdOffset = index;
        break;
      }
    }
    if (eocdOffset < 0) throw new Error("invalid ZIP: EOCD not found");
    const count = tail.readUInt16LE(eocdOffset + 10);
    const centralOffset = tail.readUInt32LE(eocdOffset + 16);
    let cursor = centralOffset;
    const entries = [];
    for (let index = 0; index < count; index += 1) {
      const fixed = Buffer.alloc(46);
      fs.readSync(fd, fixed, 0, fixed.length, cursor);
      if (fixed.readUInt32LE(0) !== 0x02014b50) {
        throw new Error("invalid ZIP: central directory entry");
      }
      const method = fixed.readUInt16LE(10);
      if (method !== 0) throw new Error("unsupported ZIP compression method");
      const compressedSize = fixed.readUInt32LE(20);
      const size = fixed.readUInt32LE(24);
      if (compressedSize !== size) throw new Error("invalid stored ZIP entry");
      const nameLength = fixed.readUInt16LE(28);
      const extraLength = fixed.readUInt16LE(30);
      const commentLength = fixed.readUInt16LE(32);
      const nameBytes = Buffer.alloc(nameLength);
      fs.readSync(fd, nameBytes, 0, nameLength, cursor + fixed.length);
      const name = assertArchivePath(nameBytes.toString("utf8"));
      entries.push({
        name,
        crc: fixed.readUInt32LE(16),
        size,
        localOffset: fixed.readUInt32LE(42),
      });
      cursor += fixed.length + nameLength + extraLength + commentLength;
    }
    return entries;
  } finally {
    fs.closeSync(fd);
  }
}

function extractStoreZip(zipPath, outputDirectory) {
  const entries = readCentralDirectory(zipPath);
  const seen = new Set();
  const zipFd = fs.openSync(zipPath, "r");
  try {
    for (const entry of entries) {
      if (seen.has(entry.name)) throw new Error(`duplicate ZIP entry: ${entry.name}`);
      seen.add(entry.name);
      const target = path.join(outputDirectory, ...entry.name.split("/"));
      const relative = path.relative(outputDirectory, target);
      if (relative.startsWith("..") || path.isAbsolute(relative)) {
        throw new Error(`unsafe ZIP target: ${entry.name}`);
      }
      const local = Buffer.alloc(30);
      fs.readSync(zipFd, local, 0, local.length, entry.localOffset);
      if (local.readUInt32LE(0) !== 0x04034b50) throw new Error("invalid ZIP local header");
      const nameLength = local.readUInt16LE(26);
      const extraLength = local.readUInt16LE(28);
      const dataOffset = entry.localOffset + local.length + nameLength + extraLength;
      fs.mkdirSync(path.dirname(target), { recursive: true });
      const outFd = fs.openSync(target, "wx");
      let remaining = entry.size;
      let readOffset = dataOffset;
      const buffer = Buffer.allocUnsafe(1024 * 1024);
      let crc = 0xffffffff;
      try {
        while (remaining > 0) {
          const requested = Math.min(buffer.length, remaining);
          const bytesRead = fs.readSync(zipFd, buffer, 0, requested, readOffset);
          if (bytesRead === 0) throw new Error(`truncated ZIP entry: ${entry.name}`);
          writeBuffer(outFd, buffer.subarray(0, bytesRead));
          for (let index = 0; index < bytesRead; index += 1) {
            crc = CRC_TABLE[(crc ^ buffer[index]) & 0xff] ^ (crc >>> 8);
          }
          remaining -= bytesRead;
          readOffset += bytesRead;
        }
      } finally {
        fs.closeSync(outFd);
      }
      if (((crc ^ 0xffffffff) >>> 0) !== entry.crc) {
        throw new Error(`CRC mismatch: ${entry.name}`);
      }
    }
  } catch (error) {
    fs.rmSync(outputDirectory, { recursive: true, force: true });
    throw error;
  } finally {
    fs.closeSync(zipFd);
  }
}

module.exports = {
  assertArchivePath,
  createStoreZip,
  extractStoreZip,
  readCentralDirectory,
};

package com.echodesk.app;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Properties;
import java.util.UUID;

/**
 * App-private atomic audio queue.
 *
 * Each record is committed by renaming a fully-written temporary directory.
 * A crash therefore leaves either a complete queue record or an ignorable
 * .tmp directory, never a half-visible WAV.
 */
final class NativeCaptureQueue {
  static final long DEFAULT_MAX_BYTES = 512L * 1024L * 1024L;
  static final long DEFAULT_RETENTION_MS = 72L * 60L * 60L * 1000L;

  static final class Record {
    final File directory;
    final File audio;
    final Properties metadata;

    Record(File directory, File audio, Properties metadata) {
      this.directory = directory;
      this.audio = audio;
      this.metadata = metadata;
    }

    String id() {
      return metadata.getProperty("segmentId", directory.getName());
    }
  }

  private final File root;
  private final long maxBytes;
  private final long retentionMs;

  NativeCaptureQueue(File root) {
    this(root, DEFAULT_MAX_BYTES, DEFAULT_RETENTION_MS);
  }

  NativeCaptureQueue(File root, long maxBytes, long retentionMs) {
    this.root = root;
    this.maxBytes = maxBytes;
    this.retentionMs = retentionMs;
  }

  synchronized Record enqueue(byte[] wav, Properties metadata) throws IOException {
    ensureRoot();
    cleanup(System.currentTimeMillis());
    String id =
        metadata.getProperty(
            "segmentId",
            "native-" + System.currentTimeMillis() + "-" + UUID.randomUUID()
        );
    metadata.setProperty("segmentId", id);
    metadata.setProperty(
        "createdAt",
        metadata.getProperty("createdAt", String.valueOf(System.currentTimeMillis()))
    );
    File temporary = new File(root, ".tmp-" + UUID.randomUUID());
    File committed = new File(
        root,
        "q-" + metadata.getProperty("createdAt") + "-" + safeFilePart(id)
    );
    if (!temporary.mkdirs()) {
      throw new IOException("capture queue temp directory could not be created");
    }
    boolean success = false;
    try {
      writeAndSync(new File(temporary, "audio.wav"), wav);
      File meta = new File(temporary, "meta.properties");
      try (FileOutputStream output = new FileOutputStream(meta)) {
        metadata.store(output, "EchoDesk native capture queue");
        output.getFD().sync();
      }
      if (!temporary.renameTo(committed)) {
        throw new IOException("capture queue atomic commit failed");
      }
      success = true;
    } finally {
      if (!success) deleteRecursively(temporary);
    }
    cleanup(System.currentTimeMillis());
    return readRecord(committed);
  }

  synchronized List<Record> records() {
    List<Record> result = new ArrayList<>();
    if (!root.isDirectory()) return result;
    File[] children = root.listFiles();
    if (children == null) return result;
    for (File child : children) {
      if (!child.isDirectory() || !child.getName().startsWith("q-")) continue;
      try {
        result.add(readRecord(child));
      } catch (IOException ignored) {
        // Invalid records are removed by cleanup; never present partial data.
      }
    }
    result.sort(Comparator.comparingLong(record -> record.directory.lastModified()));
    return result;
  }

  synchronized void remove(Record record) {
    deleteRecursively(record.directory);
  }

  synchronized int count() {
    return records().size();
  }

  synchronized long bytes() {
    long total = 0;
    for (Record record : records()) {
      total += record.audio.length();
    }
    return total;
  }

  synchronized void cleanup(long now) {
    if (!root.isDirectory()) return;
    File[] children = root.listFiles();
    if (children == null) return;
    for (File child : children) {
      if (child.getName().startsWith(".tmp-")) {
        deleteRecursively(child);
      }
    }
    List<Record> current = recordsWithoutCleanup();
    for (Record record : current) {
      long createdAt = parseLong(
          record.metadata.getProperty("createdAt"),
          record.directory.lastModified()
      );
      if (now - createdAt > retentionMs) {
        deleteRecursively(record.directory);
      }
    }
    current = recordsWithoutCleanup();
    long total = 0;
    for (Record record : current) total += record.audio.length();
    for (Record record : current) {
      if (total <= maxBytes) break;
      long size = record.audio.length();
      deleteRecursively(record.directory);
      total -= size;
    }
  }

  private List<Record> recordsWithoutCleanup() {
    List<Record> result = new ArrayList<>();
    File[] children = root.listFiles();
    if (children == null) return result;
    for (File child : children) {
      if (!child.isDirectory() || !child.getName().startsWith("q-")) continue;
      try {
        result.add(readRecord(child));
      } catch (IOException error) {
        deleteRecursively(child);
      }
    }
    result.sort(
        Comparator.comparingLong(
            record ->
                parseLong(
                    record.metadata.getProperty("createdAt"),
                    record.directory.lastModified()
                )
        )
    );
    return result;
  }

  private Record readRecord(File directory) throws IOException {
    File audio = new File(directory, "audio.wav");
    File meta = new File(directory, "meta.properties");
    if (!audio.isFile() || !meta.isFile()) {
      throw new IOException("incomplete capture queue record");
    }
    Properties properties = new Properties();
    try (InputStream input = new FileInputStream(meta)) {
      properties.load(input);
    }
    return new Record(directory, audio, properties);
  }

  private void ensureRoot() throws IOException {
    if (root.isDirectory()) return;
    if (!root.mkdirs() && !root.isDirectory()) {
      throw new IOException("capture queue directory could not be created");
    }
  }

  private static void writeAndSync(File target, byte[] bytes) throws IOException {
    try (FileOutputStream output = new FileOutputStream(target)) {
      output.write(bytes);
      output.getFD().sync();
    }
  }

  static void copy(InputStream input, OutputStream output) throws IOException {
    byte[] buffer = new byte[16 * 1024];
    int count;
    while ((count = input.read(buffer)) >= 0) {
      if (count > 0) output.write(buffer, 0, count);
    }
  }

  private static String safeFilePart(String value) {
    return value.replaceAll("[^A-Za-z0-9._-]", "_");
  }

  private static long parseLong(String value, long fallback) {
    try {
      return Long.parseLong(value);
    } catch (Exception ignored) {
      return fallback;
    }
  }

  static void deleteRecursively(File file) {
    if (file == null || !file.exists()) return;
    if (file.isDirectory()) {
      File[] children = file.listFiles();
      if (children != null) {
        for (File child : children) deleteRecursively(child);
      }
    }
    // Best effort; the next cleanup pass retries if deletion is interrupted.
    file.delete();
  }
}

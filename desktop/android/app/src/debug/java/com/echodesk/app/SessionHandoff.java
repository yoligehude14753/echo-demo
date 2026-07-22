package com.echodesk.app;

import android.content.Context;
import android.os.Process;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Debug-only, app-private bridge for task-owned instrumentation.
 *
 * <p>The renderer already passes a short-lived session to the native Capacitor
 * plugin. This handoff makes that same in-memory boundary observable to an
 * instrumentation process without adding an exported component or logging a
 * credential. It is one-shot and expires quickly; release uses the no-op
 * implementation in {@code src/release}.</p>
 */
final class SessionHandoff {
  private static final String FILE_NAME = "echodesk-debug-session.handoff";
  private static final String MAGIC = "ECHODESK_SESSION_V1";
  private static final long TTL_MS = 60_000L;
  private static final int MAX_FILE_BYTES = 8 * 1024;
  private static final int MAX_FIELD_BYTES = 4 * 1024;

  static final class Credentials {
    final String baseUrl;
    final String bearerToken;
    final String deviceId;

    Credentials(String baseUrl, String bearerToken, String deviceId) {
      this.baseUrl = baseUrl;
      this.bearerToken = bearerToken;
      this.deviceId = deviceId;
    }
  }

  private SessionHandoff() {}

  static void publish(Context context, String baseUrl, String bearerToken, String deviceId) {
    String normalizedBaseUrl = normalize(baseUrl);
    String normalizedBearerToken = normalize(bearerToken);
    String normalizedDeviceId = normalize(deviceId);
    if (
        normalizedBaseUrl.isEmpty()
            || normalizedBearerToken.isEmpty()
            || normalizedDeviceId.isEmpty()
    ) {
      return;
    }
    try {
      byte[] payload = encode(
          System.currentTimeMillis() + TTL_MS,
          normalizedBaseUrl,
          normalizedBearerToken,
          normalizedDeviceId
      );
      File directory = context.getCacheDir();
      File target = new File(directory, FILE_NAME);
      File temporary = new File(directory, FILE_NAME + ".tmp-" + Process.myPid());
      writePrivate(temporary, payload);
      // The target is a single latest-value slot. Removing the previous slot
      // before rename keeps a stale credential from surviving a failed write.
      if (target.exists() && !target.delete()) {
        temporary.delete();
        return;
      }
      if (!temporary.renameTo(target)) {
        temporary.delete();
        return;
      }
      restrictToOwner(target);
    } catch (Exception ignored) {
      // Diagnostics must never affect the authenticated capture path.
    }
  }

  static Credentials consume(Context context) {
    File directory = context.getCacheDir();
    File target = new File(directory, FILE_NAME);
    File claimed = new File(
        directory,
        FILE_NAME + ".claimed-" + Process.myPid() + "-" + System.nanoTime()
    );
    if (!target.renameTo(claimed)) return null;
    try {
      restrictToOwner(claimed);
      byte[] payload = readBounded(claimed);
      return decode(payload);
    } catch (Exception ignored) {
      return null;
    } finally {
      claimed.delete();
    }
  }

  static void clear(Context context) {
    File directory = context.getCacheDir();
    new File(directory, FILE_NAME).delete();
    File[] leftovers = directory.listFiles();
    if (leftovers == null) return;
    for (File file : leftovers) {
      if (file.getName().startsWith(FILE_NAME + ".")) file.delete();
    }
  }

  private static byte[] encode(
      long expiresAt,
      String baseUrl,
      String bearerToken,
      String deviceId
  ) throws Exception {
    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
    DataOutputStream output = new DataOutputStream(bytes);
    output.writeUTF(MAGIC);
    output.writeLong(expiresAt);
    writeField(output, baseUrl);
    writeField(output, bearerToken);
    writeField(output, deviceId);
    output.flush();
    return bytes.toByteArray();
  }

  private static Credentials decode(byte[] payload) throws Exception {
    if (payload == null || payload.length == 0 || payload.length > MAX_FILE_BYTES) return null;
    DataInputStream input = new DataInputStream(new ByteArrayInputStream(payload));
    if (!MAGIC.equals(input.readUTF())) return null;
    if (input.readLong() <= System.currentTimeMillis()) return null;
    String baseUrl = readField(input);
    String bearerToken = readField(input);
    String deviceId = readField(input);
    if (input.available() != 0) return null;
    if (baseUrl.isEmpty() || bearerToken.isEmpty() || deviceId.isEmpty()) return null;
    return new Credentials(baseUrl, bearerToken, deviceId);
  }

  private static void writeField(DataOutputStream output, String value) throws Exception {
    byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
    if (bytes.length == 0 || bytes.length > MAX_FIELD_BYTES) throw new IllegalArgumentException();
    output.writeInt(bytes.length);
    output.write(bytes);
  }

  private static String readField(DataInputStream input) throws Exception {
    int length = input.readInt();
    if (length <= 0 || length > MAX_FIELD_BYTES || length > input.available()) {
      throw new IllegalArgumentException();
    }
    byte[] bytes = new byte[length];
    input.readFully(bytes);
    return new String(bytes, StandardCharsets.UTF_8).trim();
  }

  private static byte[] readBounded(File file) throws Exception {
    if (file.length() <= 0 || file.length() > MAX_FILE_BYTES) return null;
    try (FileInputStream input = new FileInputStream(file)) {
      byte[] payload = new byte[(int) file.length()];
      int offset = 0;
      while (offset < payload.length) {
        int count = input.read(payload, offset, payload.length - offset);
        if (count < 0) throw new IllegalArgumentException();
        offset += count;
      }
      return payload;
    }
  }

  private static void writePrivate(File file, byte[] payload) throws Exception {
    restrictToOwner(file);
    try (FileOutputStream output = new FileOutputStream(file, false)) {
      output.write(payload);
      output.flush();
      output.getFD().sync();
    }
    restrictToOwner(file);
  }

  private static void restrictToOwner(File file) {
    file.setReadable(false, false);
    file.setWritable(false, false);
    file.setExecutable(false, false);
    file.setReadable(true, true);
    file.setWritable(true, true);
  }

  private static String normalize(String value) {
    return value == null ? "" : value.trim();
  }
}

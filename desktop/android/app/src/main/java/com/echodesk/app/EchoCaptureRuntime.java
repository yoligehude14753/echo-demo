package com.echodesk.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.List;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Process-scoped native free-capture runtime.
 *
 * The short-lived bearer token is intentionally memory-only. Audio and
 * non-secret routing metadata are durable in the app-private queue. When the
 * token expires, queued audio remains untouched until the renderer renews the
 * session and calls configureSession again.
 */
final class EchoCaptureRuntime {
  private static final String TAG = "EchoCaptureRuntime";
  private static final String PREFS = "echodesk_native_capture";
  static final String CLIENT_VERSION_HEADER = "X-EchoDesk-Client-Version";
  static final int MAX_UPLOAD_RESPONSE_BYTES = 64 * 1024;
  private static final Pattern JSON_AMBIENT_STORED = Pattern.compile(
      "\\\"ambient_stored\\\"\\s*:\\s*(true|false)"
  );
  private static final Pattern JSON_STT_STATUS = Pattern.compile(
      "\\\"stt_status\\\"\\s*:\\s*(\\\"(?:\\\\.|[^\\\"\\\\])*\\\"|null)"
  );
  private static final Pattern JSON_AMBIENT_TEXT = Pattern.compile(
      "\\\"ambient_text\\\"\\s*:\\s*(\\\"(?:\\\\.|[^\\\"\\\\])*\\\"|null)"
  );
  private static final String KEY_FREE_MODE = "free_mode_enabled";
  private static final String KEY_PAUSED = "free_mode_paused";
  private static final String KEY_BASE_URL = "base_url";
  private static final String KEY_DEVICE_ID = "device_id";
  private static final long RETRY_MS = 15_000L;
  private static volatile EchoCaptureRuntime instance;

  static EchoCaptureRuntime get(Context context) {
    EchoCaptureRuntime current = instance;
    if (current != null) return current;
    synchronized (EchoCaptureRuntime.class) {
      current = instance;
      if (current == null) {
        current = new EchoCaptureRuntime(context.getApplicationContext());
        instance = current;
      }
      return current;
    }
  }

  private final Context context;
  private final SharedPreferences preferences;
  private final NativeCaptureQueue queue;
  private final NativeAudioGate gate = new NativeAudioGate();
  private final ScheduledExecutorService uploader =
      Executors.newSingleThreadScheduledExecutor(
          runnable -> {
            Thread thread = new Thread(runnable, "EchoDeskNativeUpload");
            thread.setDaemon(true);
            return thread;
          }
      );
  private final AtomicBoolean drainRunning = new AtomicBoolean(false);
  private volatile String baseUrl;
  private volatile String bearerToken;
  private volatile String deviceId;
  private volatile String correlationSalt;
  private volatile boolean formalMode;
  private volatile String meetingId;
  private volatile boolean authBlocked;
  private volatile int recoveryAttempts;

  static final class UploadResponse {
    final int status;
    final boolean ambientStored;
    final String sttStatus;
    final String textSha256;
    final String ambientText;
    final String segmentCorrelation;

    UploadResponse(int status, boolean ambientStored, String sttStatus, String textSha256) {
      this(status, ambientStored, sttStatus, textSha256, null, null);
    }

    UploadResponse(
        int status,
        boolean ambientStored,
        String sttStatus,
        String textSha256,
        String ambientText,
        String segmentCorrelation
    ) {
      this.status = status;
      this.ambientStored = ambientStored;
      this.sttStatus = sttStatus;
      this.textSha256 = textSha256;
      this.ambientText = ambientText;
      this.segmentCorrelation = segmentCorrelation;
    }

    UploadResponse withSegmentCorrelation(String correlation) {
      return new UploadResponse(
          status,
          ambientStored,
          sttStatus,
          textSha256,
          ambientText,
          correlation
      );
    }
  }

  private EchoCaptureRuntime(Context context) {
    this.context = context;
    this.preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    this.queue = new NativeCaptureQueue(
        new File(context.getFilesDir(), "native-capture-queue")
    );
    this.baseUrl = normalizeBaseUrl(preferences.getString(KEY_BASE_URL, ""));
    this.deviceId = normalizeSessionField(preferences.getString(KEY_DEVICE_ID, ""));
    this.correlationSalt = "";
  }

  synchronized void configureSession(
      String baseUrl,
      String bearerToken,
      String deviceId,
      String correlationSalt
  ) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.bearerToken = bearerToken == null ? "" : bearerToken.trim();
    this.deviceId = deviceId == null ? "" : deviceId.trim();
    this.correlationSalt = correlationSalt == null ? "" : correlationSalt.trim();
    this.authBlocked = false;
    preferences
        .edit()
        .putString(KEY_BASE_URL, this.baseUrl)
        .putString(KEY_DEVICE_ID, this.deviceId)
        .apply();
    requestDrain();
  }

  // Retain the package-local test seam for legacy native transport tests; the
  // product Capacitor path always supplies the renderer correlation salt.
  synchronized void configureSession(
      String baseUrl,
      String bearerToken,
      String deviceId
  ) {
    configureSession(baseUrl, bearerToken, deviceId, "");
  }

  synchronized void clearSession() {
    this.bearerToken = "";
    this.correlationSalt = "";
    this.authBlocked = true;
  }

  void markFreeModeEnabled(boolean enabled) {
    preferences.edit().putBoolean(KEY_FREE_MODE, enabled).apply();
  }

  boolean isFreeModeEnabled() {
    return preferences.getBoolean(KEY_FREE_MODE, false);
  }

  void setPaused(boolean paused) {
    preferences.edit().putBoolean(KEY_PAUSED, paused).apply();
  }

  boolean isPaused() {
    return preferences.getBoolean(KEY_PAUSED, false);
  }

  void setFormalMode(boolean formalMode, String meetingId) {
    this.formalMode = formalMode;
    this.meetingId =
        formalMode && meetingId != null && !meetingId.isBlank()
            ? meetingId.trim()
            : "";
  }

  boolean isFormalMode() {
    return formalMode;
  }

  boolean isAuthBlocked() {
    return authBlocked;
  }

  boolean hasUploadSession() {
    return hasUploadSession(baseUrl, bearerToken, deviceId);
  }

  static boolean hasUploadSession(
      String baseUrl,
      String bearerToken,
      String deviceId
  ) {
    return !normalizeBaseUrl(baseUrl).isBlank()
        && !normalizeSessionField(bearerToken).isBlank()
        && !normalizeSessionField(deviceId).isBlank();
  }

  static boolean canQueueNativeCapture(
      String baseUrl,
      String bearerToken,
      String deviceId,
      boolean authBlocked
  ) {
    return !authBlocked && hasUploadSession(baseUrl, bearerToken, deviceId);
  }

  int queuedCount() {
    return queue.count();
  }

  long queuedBytes() {
    return queue.bytes();
  }

  boolean acceptPcm(
      byte[] pcm,
      int sampleRate,
      String source,
      double rms,
      int peak
  ) {
    return acceptPcm(pcm, sampleRate, source);
  }

  boolean acceptPcm(byte[] pcm, int sampleRate, String source) {
    if (isPaused()) return true;
    if (!canQueueNativeCapture(baseUrl, bearerToken, deviceId, authBlocked)) {
      EchoCaptureService.updateUploadState(context, "auth_required", queue.count());
      return false;
    }
    List<NativeAudioGate.Result> segments = gate.process(
        pcm,
        sampleRate,
        formalMode,
        meetingId
    );
    return enqueueGatedSegments(segments, source);
  }

  boolean finishPcm(String source) {
    if (isPaused()) {
      gate.reset();
      return true;
    }
    if (!canQueueNativeCapture(baseUrl, bearerToken, deviceId, authBlocked)) {
      gate.reset();
      EchoCaptureService.updateUploadState(context, "auth_required", queue.count());
      return false;
    }
    return enqueueGatedSegments(gate.finish(), source);
  }

  private boolean enqueueGatedSegments(
      List<NativeAudioGate.Result> segments,
      String source
  ) {
    if (segments.isEmpty()) {
      Log.d(
          TAG,
          "native capture gated: no completed 20ms VAD segment"
      );
      return hasUploadSession();
    }
    for (NativeAudioGate.Result segment : segments) {
      if (!enqueueGatedSegment(segment, source)) return false;
    }
    return true;
  }

  private boolean enqueueGatedSegment(
      NativeAudioGate.Result gated,
      String source
  ) {
    String currentDeviceId = deviceId;
    if (currentDeviceId == null || currentDeviceId.isBlank()) {
      // Until the authenticated session is known, keep the legacy JS path.
      return false;
    }
    String segmentId =
        currentDeviceId
            + ":native:"
            + System.currentTimeMillis()
            + ":"
            + UUID.randomUUID();
    Properties metadata = new Properties();
    metadata.setProperty("segmentId", segmentId);
    metadata.setProperty("deviceId", currentDeviceId);
    metadata.setProperty("sampleRate", String.valueOf(gated.sampleRate));
    metadata.setProperty("formalMode", String.valueOf(gated.formalMode));
    metadata.setProperty("meetingId", gated.formalMode ? safe(gated.meetingId) : "");
    metadata.setProperty("source", safe(source));
    metadata.setProperty("rms", String.valueOf(gated.rms));
    metadata.setProperty("peak", String.valueOf(gated.peak));
    // 队列元数据不含密钥，可逐上传回答“此设备发送的是短 VAD 段还是长整窗”。
    metadata.setProperty("captureCadenceMs", String.valueOf(NativeAudioGate.DEFAULT_FRAME_MS));
    metadata.setProperty("segmentDurationMs", String.valueOf(gated.durationMs));
    metadata.setProperty("observedFrames", String.valueOf(gated.observedFrames));
    metadata.setProperty("speechFrames", String.valueOf(gated.speechFrames));
    metadata.setProperty("speechFrameRatio", String.valueOf(gated.speechFrameRatio));
    try {
      byte[] wav = EchoAudioPlugin.wavBytesForNativeQueue(gated.pcm, gated.sampleRate);
      queue.enqueue(wav, metadata);
      EchoCaptureService.updateQueueState(
          context,
          queue.count(),
          gated.formalMode,
          isPaused()
      );
      Log.i(
          TAG,
          "native_capture_segment {\"mode\":\""
              + (gated.formalMode ? "formal" : "free")
              + "\",\"duration_ms\":"
              + gated.durationMs
              + ",\"vad_frame_ms\":"
              + NativeAudioGate.DEFAULT_FRAME_MS
              + ",\"observed_frames\":"
              + gated.observedFrames
              + ",\"speech_frames\":"
              + gated.speechFrames
              + ",\"speech_frame_ratio\":"
              + gated.speechFrameRatio
              + "}"
      );
      requestDrain();
      return true;
    } catch (Exception error) {
      Log.e(TAG, "native audio queue commit failed", error);
      return false;
    }
  }

  void requestDrain() {
    if (authBlocked || !hasUploadSession()) {
      EchoCaptureService.updateUploadState(
          context,
          authBlocked ? "auth_blocked" : "auth_required",
          queue.count()
      );
      return;
    }
    if (!drainRunning.compareAndSet(false, true)) return;
    uploader.execute(this::drain);
  }

  private void drain() {
    boolean retry = false;
    try {
      List<NativeCaptureQueue.Record> records = queue.records();
      for (NativeCaptureQueue.Record record : records) {
        if (authBlocked || !hasUploadSession()) break;
        int status = upload(record);
        if (status >= 200 && status < 300) {
          recoveryAttempts = 0;
          queue.remove(record);
          EchoCaptureService.updateQueueState(
              context,
              queue.count(),
              formalMode,
              isPaused()
          );
          continue;
        }
        if (status == 401 || status == 403 || status == 409) {
          authBlocked = true;
          recoveryAttempts += 1;
          EchoCaptureService.updateUploadState(
              context,
              status == 409 ? "selection_blocked" : "auth_blocked",
              queue.count()
          );
          if (recoveryAttempts <= 3) {
            EchoAudioPlugin.notifyUploadRecoveryRequired(status);
          } else {
            EchoCaptureService.updateUploadState(context, "auth_blocked", queue.count());
          }
          Log.w(TAG, "native upload waiting for renewed authenticated session: " + status);
          break;
        }
        retry = true;
        Log.w(TAG, "native upload retained for retry: HTTP " + status);
        break;
      }
    } catch (Exception error) {
      retry = true;
      Log.w(TAG, "native upload retained after transport failure", error);
    } finally {
      drainRunning.set(false);
      if (retry && !authBlocked && hasUploadSession()) {
        uploader.schedule(this::requestDrain, RETRY_MS, TimeUnit.MILLISECONDS);
      }
    }
  }

  private int upload(NativeCaptureQueue.Record record) throws Exception {
    String boundary = "EchoDeskNative" + UUID.randomUUID().toString().replace("-", "");
    URL endpoint = new URL(normalizeBaseUrl(baseUrl) + "/capture/chunk");
    HttpURLConnection connection = (HttpURLConnection) endpoint.openConnection();
    connection.setRequestMethod("POST");
    connection.setConnectTimeout(10_000);
    connection.setReadTimeout(45_000);
    connection.setDoOutput(true);
    connection.setInstanceFollowRedirects(false);
    connection.setRequestProperty("Authorization", "Bearer " + bearerToken);
    connection.setRequestProperty("Idempotency-Key", "capture:" + record.id());
    connection.setRequestProperty("X-Capture-Device-Id", record.metadata.getProperty("deviceId", ""));
    connection.setRequestProperty("X-Echo-Platform", "android");
    connection.setRequestProperty(CLIENT_VERSION_HEADER, appVersion());
    connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
    try (OutputStream output = connection.getOutputStream()) {
      writeField(output, boundary, "sample_rate", record.metadata.getProperty("sampleRate", "16000"));
      writeField(output, boundary, "deviceId", record.metadata.getProperty("deviceId", ""));
      writeField(output, boundary, "segmentId", record.id());
      String queuedMeetingId = record.metadata.getProperty("meetingId", "");
      if (!queuedMeetingId.isBlank()) {
        writeField(output, boundary, "meeting_id", queuedMeetingId);
      }
      writeField(
          output,
          boundary,
          "capture_mode",
          Boolean.parseBoolean(record.metadata.getProperty("formalMode", "false"))
              ? "formal"
              : "free"
      );
      writeFile(output, boundary, "audio", record.audio);
      output.write(("--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
    }
    int status = connection.getResponseCode();
    String responseBody = "";
    try (InputStream input = status >= 400
        ? connection.getErrorStream()
        : connection.getInputStream()) {
      responseBody = readBoundedResponse(input);
    } catch (Exception ignored) {
      // Status remains authoritative for queue ownership; body telemetry is best effort.
    } finally {
      connection.disconnect();
    }
    UploadResponse response = parseUploadResponse(status, responseBody)
        .withSegmentCorrelation(correlationForSegmentId(record.id(), correlationSalt));
    if (status >= 200 && status < 300) {
      logSuccessfulUpload(record, response);
      EchoAudioPlugin.notifyUploadSucceeded(response);
    }
    return status;
  }

  static String readBoundedResponse(InputStream input) throws Exception {
    if (input == null) return "";
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    byte[] buffer = new byte[8 * 1024];
    int total = 0;
    int count;
    while ((count = input.read(buffer)) >= 0) {
      if (count == 0) continue;
      int remaining = MAX_UPLOAD_RESPONSE_BYTES - total;
      if (remaining <= 0) break;
      int accepted = Math.min(count, remaining);
      output.write(buffer, 0, accepted);
      total += accepted;
      if (accepted < count) break;
    }
    return output.toString(StandardCharsets.UTF_8.name());
  }

  static UploadResponse parseUploadResponse(int status, String body) {
    boolean ambientStored = false;
    String sttStatus = "unknown";
    String textSha256 = "";
    String ambientText = null;
    if (status >= 200 && status < 300 && body != null && !body.isBlank()) {
      ambientStored = readJsonBoolean(JSON_AMBIENT_STORED, body);
      sttStatus = normalizeSttStatus(readJsonString(JSON_STT_STATUS, body));
      String text = readJsonString(JSON_AMBIENT_TEXT, body);
      if (text != null) {
        ambientText = text;
        textSha256 = sha256(text);
      }
    }
    return new UploadResponse(status, ambientStored, sttStatus, textSha256, ambientText, null);
  }

  private static boolean readJsonBoolean(Pattern pattern, String body) {
    Matcher match = pattern.matcher(body);
    return match.find() && "true".equals(match.group(1));
  }

  private static String readJsonString(Pattern pattern, String body) {
    Matcher match = pattern.matcher(body);
    if (!match.find() || "null".equals(match.group(1))) return null;
    return unescapeJsonString(match.group(1));
  }

  private static String unescapeJsonString(String quoted) {
    String value = quoted.substring(1, quoted.length() - 1);
    StringBuilder result = new StringBuilder(value.length());
    for (int index = 0; index < value.length(); index++) {
      char current = value.charAt(index);
      if (current != '\\' || index + 1 >= value.length()) {
        result.append(current);
        continue;
      }
      char escaped = value.charAt(++index);
      switch (escaped) {
        case '"': result.append('"'); break;
        case '\\': result.append('\\'); break;
        case '/': result.append('/'); break;
        case 'b': result.append('\b'); break;
        case 'f': result.append('\f'); break;
        case 'n': result.append('\n'); break;
        case 'r': result.append('\r'); break;
        case 't': result.append('\t'); break;
        case 'u':
          if (index + 4 >= value.length()) return "";
          try {
            result.append((char) Integer.parseInt(value.substring(index + 1, index + 5), 16));
            index += 4;
          } catch (NumberFormatException error) {
            return "";
          }
          break;
        default: return "";
      }
    }
    return result.toString();
  }

  private static void logSuccessfulUpload(
      NativeCaptureQueue.Record record,
      UploadResponse response
  ) {
    Log.i(
        TAG,
        "native_capture_result {\"status\":"
            + response.status
            + ",\"ambient_stored\":"
            + response.ambientStored
            + ",\"stt_status\":\""
            + response.sttStatus
            + "\"}"
    );
  }

  /** Same session-salted UTF-16 hash as desktop/src/capture/captureCorrelation.ts. */
  static String correlationForSegmentId(String segmentId, String salt) {
    if (segmentId == null || segmentId.trim().isEmpty()) return null;
    if (salt == null || salt.trim().isEmpty()) return null;
    String value = salt + "\u0000" + segmentId;
    int first = 0x811c9dc5;
    int second = 0x9e3779b9;
    for (int index = 0; index < value.length(); index += 1) {
      int code = value.charAt(index);
      first ^= code;
      first *= 0x01000193;
      second ^= code + index;
      second *= 0x85ebca6b;
    }
    return "seg-" + hex32(first) + hex32(second);
  }

  private static String hex32(int value) {
    String hex = Integer.toUnsignedString(value, 16);
    StringBuilder padded = new StringBuilder(8);
    for (int index = hex.length(); index < 8; index += 1) padded.append('0');
    padded.append(hex);
    return padded.toString();
  }

  private static String normalizeSttStatus(String value) {
    if (
        "ok".equals(value)
            || "empty".equals(value)
            || "failed".equals(value)
            || "circuit_open".equals(value)
            || "gated".equals(value)
            || "unknown".equals(value)
    ) {
      return value;
    }
    return "unknown";
  }

  private static String sha256(String value) {
    try {
      byte[] digest = MessageDigest.getInstance("SHA-256")
          .digest(value.getBytes(StandardCharsets.UTF_8));
      StringBuilder hex = new StringBuilder(digest.length * 2);
      for (byte item : digest) {
        hex.append(String.format("%02x", item & 0xff));
      }
      return hex.toString();
    } catch (NoSuchAlgorithmException impossible) {
      return "unavailable";
    }
  }

  private static void writeField(
      OutputStream output,
      String boundary,
      String name,
      String value
  ) throws Exception {
    output.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.UTF_8));
    output.write(
        ("Content-Disposition: form-data; name=\"" + name + "\"\r\n\r\n")
            .getBytes(StandardCharsets.UTF_8)
    );
    output.write(value.getBytes(StandardCharsets.UTF_8));
    output.write("\r\n".getBytes(StandardCharsets.UTF_8));
  }

  private static void writeFile(
      OutputStream output,
      String boundary,
      String name,
      File file
  ) throws Exception {
    output.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.UTF_8));
    output.write(
        (
                "Content-Disposition: form-data; name=\""
                    + name
                    + "\"; filename=\"chunk.wav\"\r\n"
            )
            .getBytes(StandardCharsets.UTF_8)
    );
    output.write("Content-Type: audio/wav\r\n\r\n".getBytes(StandardCharsets.UTF_8));
    try (BufferedInputStream input = new BufferedInputStream(new FileInputStream(file))) {
      NativeCaptureQueue.copy(input, output);
    }
    output.write("\r\n".getBytes(StandardCharsets.UTF_8));
  }

  private static String normalizeBaseUrl(String value) {
    String normalized = value == null ? "" : value.trim();
    while (normalized.endsWith("/")) {
      normalized = normalized.substring(0, normalized.length() - 1);
    }
    return normalized;
  }

  private static String normalizeSessionField(String value) {
    return value == null ? "" : value.trim();
  }

  private String appVersion() {
    try {
      PackageInfo info =
          context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
      return info.versionName == null ? "unknown" : info.versionName;
    } catch (Exception ignored) {
      return "unknown";
    }
  }

  private static String safe(String value) {
    return value == null ? "" : value;
  }
}

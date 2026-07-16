package com.echodesk.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

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
  private volatile boolean formalMode;
  private volatile String meetingId;
  private volatile boolean authBlocked;

  private EchoCaptureRuntime(Context context) {
    this.context = context;
    this.preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    this.queue = new NativeCaptureQueue(
        new File(context.getFilesDir(), "native-capture-queue")
    );
    this.baseUrl = preferences.getString(KEY_BASE_URL, "");
    this.deviceId = preferences.getString(KEY_DEVICE_ID, "");
  }

  synchronized void configureSession(
      String baseUrl,
      String bearerToken,
      String deviceId
  ) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.bearerToken = bearerToken == null ? "" : bearerToken.trim();
    this.deviceId = deviceId == null ? "" : deviceId.trim();
    this.authBlocked = false;
    preferences
        .edit()
        .putString(KEY_BASE_URL, this.baseUrl)
        .putString(KEY_DEVICE_ID, this.deviceId)
        .apply();
    requestDrain();
  }

  synchronized void clearSession() {
    this.bearerToken = "";
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

  boolean hasUploadSession() {
    return !baseUrl.isBlank() && !bearerToken.isBlank() && !deviceId.isBlank();
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
    if (isPaused()) return true;
    boolean currentFormal = formalMode;
    NativeAudioGate.Result gated = gate.process(pcm, sampleRate, currentFormal);
    if (!gated.accepted) {
      Log.d(
          TAG,
          "free chunk gated speechFrames="
              + gated.speechFrames
              + "/"
              + gated.observedFrames
              + " rms="
              + Math.round(gated.rms)
              + " peak="
              + gated.peak
      );
      return hasUploadSession();
    }
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
    metadata.setProperty("sampleRate", String.valueOf(sampleRate));
    metadata.setProperty("formalMode", String.valueOf(currentFormal));
    metadata.setProperty("meetingId", currentFormal ? safe(meetingId) : "");
    metadata.setProperty("source", safe(source));
    metadata.setProperty("rms", String.valueOf(rms));
    metadata.setProperty("peak", String.valueOf(peak));
    try {
      byte[] wav = EchoAudioPlugin.wavBytesForNativeQueue(gated.pcm, sampleRate);
      queue.enqueue(wav, metadata);
      EchoCaptureService.updateQueueState(
          context,
          queue.count(),
          currentFormal,
          isPaused()
      );
      requestDrain();
      return true;
    } catch (Exception error) {
      Log.e(TAG, "native audio queue commit failed", error);
      return false;
    }
  }

  void requestDrain() {
    if (authBlocked || !hasUploadSession()) return;
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
          queue.remove(record);
          EchoCaptureService.updateQueueState(
              context,
              queue.count(),
              formalMode,
              isPaused()
          );
          continue;
        }
        if (status == 401 || status == 403) {
          authBlocked = true;
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
    connection.setRequestProperty("X-Echo-App-Version", appVersion());
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
    try (
        BufferedReader ignored =
            new BufferedReader(
                new InputStreamReader(
                    status >= 400
                        ? connection.getErrorStream()
                        : connection.getInputStream(),
                    StandardCharsets.UTF_8
                )
            )
    ) {
      while (ignored.readLine() != null) {
        // Drain for connection reuse without logging response payload.
      }
    } catch (Exception ignored) {
      // A status code is sufficient for queue ownership.
    } finally {
      connection.disconnect();
    }
    return status;
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

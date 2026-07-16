package com.echodesk.app;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Base64;
import android.util.Log;

import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.PermissionState;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

@CapacitorPlugin(
    name = "EchoAudio",
    permissions = {
        @Permission(alias = "microphone", strings = {Manifest.permission.RECORD_AUDIO}),
        @Permission(
            alias = "notifications",
            strings = {Manifest.permission.POST_NOTIFICATIONS}
        )
    }
)
public class EchoAudioPlugin extends Plugin {
  private static final String TAG = "EchoDeskAudio";
  private static final int DEFAULT_SAMPLE_RATE = 16000;
  private static final int DEFAULT_CHUNK_MS = 6000;
  private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO;
  private static final int AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT;
  private static final int PROBE_MS = 500;
  private static final double DEAD_INPUT_RMS_FLOOR = 1.0;
  private static final int DEAD_INPUT_PEAK_FLOOR = 4;
  private static final int[] AUDIO_SOURCES = {
      MediaRecorder.AudioSource.MIC,
      MediaRecorder.AudioSource.VOICE_RECOGNITION,
      MediaRecorder.AudioSource.DEFAULT,
      MediaRecorder.AudioSource.VOICE_COMMUNICATION,
      MediaRecorder.AudioSource.CAMCORDER
  };
  private static volatile EchoAudioPlugin activeInstance = null;
  private static volatile EchoAudioPlugin eventTarget = null;

  private final Object lock = new Object();
  private AudioRecord recorder = null;
  private Thread worker = null;
  private volatile boolean running = false;
  private int activeSampleRate = DEFAULT_SAMPLE_RATE;
  private String activeSource = "unknown";

  @PluginMethod
  public void configureSession(PluginCall call) {
    String baseUrl = call.getString("baseUrl", "");
    String sessionToken = call.getString("sessionToken", "");
    String deviceId = call.getString("deviceId", "");
    if (
        baseUrl == null
            || baseUrl.isBlank()
            || sessionToken == null
            || sessionToken.isBlank()
            || deviceId == null
            || deviceId.isBlank()
    ) {
      call.reject("baseUrl, sessionToken and authoritative deviceId are required");
      return;
    }
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
    runtime.configureSession(baseUrl, sessionToken, deviceId);
    JSObject result = runtimeStatus(runtime);
    call.resolve(result);
  }

  @PluginMethod
  public void clearSession(PluginCall call) {
    EchoCaptureRuntime.get(getContext()).clearSession();
    call.resolve();
  }

  @PluginMethod
  public void setCaptureMode(PluginCall call) {
    boolean formal = Boolean.TRUE.equals(call.getBoolean("formal", false));
    String meetingId = call.getString("meetingId", "");
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
    runtime.setFormalMode(formal, meetingId);
    EchoCaptureService.updateQueueState(
        getContext(),
        runtime.queuedCount(),
        formal,
        runtime.isPaused()
    );
    call.resolve(runtimeStatus(runtime));
  }

  @PluginMethod
  public void pauseFreeMode(PluginCall call) {
    EchoCaptureService.pause(getContext());
    call.resolve(runtimeStatus(EchoCaptureRuntime.get(getContext())));
  }

  @PluginMethod
  public void stopAndExit(PluginCall call) {
    EchoCaptureService.stopAndExit(getContext());
    call.resolve();
  }

  @PluginMethod
  public void start(PluginCall call) {
    if (getPermissionState("microphone") != PermissionState.GRANTED) {
      requestPermissionForAlias("microphone", call, "startAfterPermission");
      return;
    }
    if (
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && getPermissionState("notifications") != PermissionState.GRANTED
    ) {
      requestPermissionForAlias("notifications", call, "startAfterPermission");
      return;
    }
    startRecording(call);
  }

  @PermissionCallback
  private void startAfterPermission(PluginCall call) {
    if (getPermissionState("microphone") != PermissionState.GRANTED) {
      call.reject("RECORD_AUDIO not granted");
      return;
    }
    if (
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && getPermissionState("notifications") != PermissionState.GRANTED
    ) {
      requestPermissionForAlias("notifications", call, "startAfterPermission");
      return;
    }
    startRecording(call);
  }

  private void startRecording(PluginCall call) {
    int sampleRate = call.getInt("sampleRate", DEFAULT_SAMPLE_RATE);
    int chunkMs = call.getInt("chunkMs", DEFAULT_CHUNK_MS);
    if (sampleRate <= 0) sampleRate = DEFAULT_SAMPLE_RATE;
    if (chunkMs < 1000) chunkMs = DEFAULT_CHUNK_MS;

    EchoAudioPlugin existing = activeInstance;
    if (existing != null && existing.running) {
      eventTarget = this;
      EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
      runtime.markFreeModeEnabled(true);
      runtime.setPaused(false);
      JSObject result = recordingResult(existing.activeSampleRate, existing.activeSource, true);
      call.resolve(result);
      return;
    }

    try {
      EchoCaptureService.start(getContext());
    } catch (Throwable error) {
      call.reject(
          "Android microphone foreground service could not start",
          error instanceof Exception ? (Exception) error : new Exception(error)
      );
      return;
    }

    synchronized (lock) {
      stopLocked();
      AudioRecord next = null;
      String sourceName = "unknown";
      int selectedSampleRate = sampleRate;
      int fallbackSource = -1;
      int fallbackSampleRate = sampleRate;
      String fallbackSourceName = "unknown";
      StringBuilder probeSummary = new StringBuilder();
      for (int candidateRate : candidateSampleRates(sampleRate)) {
        for (int source : AUDIO_SOURCES) {
          next = buildRecorder(source, candidateRate);
          if (next == null) continue;
          try {
            next.startRecording();
          } catch (Throwable t) {
            Log.w(TAG, "AudioRecord start failed for " + sourceToName(source) + " @" + candidateRate, t);
            next.release();
            next = null;
            continue;
          }
          if (next.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
            AudioStats probeStats = probeInput(next, candidateRate);
            Log.i(
                TAG,
                "AudioRecord probe source=" + sourceToName(source)
                    + " sampleRate=" + candidateRate
                    + " rms=" + Math.round(probeStats.rms)
                    + " peak=" + probeStats.peak
            );
            if (probeSummary.length() > 0) {
              probeSummary.append("; ");
            }
            probeSummary
                .append(sourceToName(source))
                .append("@")
                .append(candidateRate)
                .append(" rms=")
                .append(Math.round(probeStats.rms))
                .append(" peak=")
                .append(probeStats.peak);
            if (isDeadInput(probeStats)) {
              Log.w(
                  TAG,
                  "AudioRecord saw silent probe source=" + sourceToName(source)
                      + " sampleRate=" + candidateRate
                      + " rms=" + Math.round(probeStats.rms)
                      + " peak=" + probeStats.peak
              );
              if (fallbackSource < 0) {
                fallbackSource = source;
                fallbackSampleRate = candidateRate;
                fallbackSourceName = sourceToName(source);
              }
              next.release();
              next = null;
              continue;
            }
            sourceName = sourceToName(source);
            selectedSampleRate = candidateRate;
            break;
          }
          next.release();
          next = null;
        }
        if (next != null) break;
      }

      if (next == null && fallbackSource >= 0) {
        next = buildRecorder(fallbackSource, fallbackSampleRate);
        if (next != null) {
          try {
            next.startRecording();
          } catch (Throwable t) {
            Log.w(TAG, "AudioRecord fallback start failed for " + fallbackSourceName + " @" + fallbackSampleRate, t);
            next.release();
            next = null;
          }
          if (next != null && next.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
            sourceName = fallbackSourceName;
            selectedSampleRate = fallbackSampleRate;
            Log.w(
                TAG,
                "AudioRecord falling back to silent probe source="
                    + sourceName
                    + " sampleRate="
                    + selectedSampleRate
                    + ". Runtime health check will report if input remains silent."
            );
          } else if (next != null) {
            next.release();
            next = null;
          }
        }
      }

      if (next == null) {
        EchoCaptureService.stop(getContext());
        String details = probeSummary.length() > 0
            ? " Probe summary: " + probeSummary + "."
            : "";
        call.reject("Android AudioRecord could not start any microphone source." + details + " Please connect a USB/Bluetooth conference microphone or enable TV microphone access.");
        return;
      }

      recorder = next;
      activeSampleRate = selectedSampleRate;
      activeSource = sourceName;
      running = true;
      Log.i(TAG, "AudioRecord started source=" + activeSource + " sampleRate=" + activeSampleRate);
      final int loopSampleRate = activeSampleRate;
      final int loopChunkMs = chunkMs;
      worker = new Thread(() -> recordLoop(loopSampleRate, loopChunkMs), "EchoDeskAudioRecord");
      worker.start();
      activeInstance = this;
      eventTarget = this;
    }

    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
    runtime.markFreeModeEnabled(true);
    runtime.setPaused(false);
    EchoCaptureService.markRecording(getContext(), activeSource);
    call.resolve(recordingResult(activeSampleRate, activeSource, false));
  }

  private JSObject recordingResult(int sampleRate, String source, boolean resumed) {
    JSObject result = new JSObject();
    result.put("sampleRate", sampleRate);
    result.put("source", source);
    result.put("resumed", resumed);
    result.put("foregroundService", EchoCaptureService.isActive());
    result.put("batteryOptimized", isBatteryOptimized());
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
    result.put("freeModeEnabled", runtime.isFreeModeEnabled());
    result.put("paused", runtime.isPaused());
    result.put("formalMode", runtime.isFormalMode());
    result.put("nativeUpload", runtime.hasUploadSession());
    result.put("queuedChunks", runtime.queuedCount());
    result.put("queuedBytes", runtime.queuedBytes());
    return result;
  }

  private static int[] candidateSampleRates(int requested) {
    int[] preferred = {requested, 48000, 44100, 16000};
    int[] tmp = new int[preferred.length];
    int n = 0;
    for (int rate : preferred) {
      if (rate <= 0) continue;
      boolean exists = false;
      for (int i = 0; i < n; i++) {
        if (tmp[i] == rate) {
          exists = true;
          break;
        }
      }
      if (!exists) {
        tmp[n] = rate;
        n += 1;
      }
    }
    int[] out = new int[n];
    System.arraycopy(tmp, 0, out, 0, n);
    return out;
  }

  @PluginMethod
  public void stop(PluginCall call) {
    // Backward-compatible explicit stop means pause. Only stopAndExit disables
    // the persisted free-mode choice and removes the notification.
    EchoCaptureService.pause(getContext());
    call.resolve();
  }

  @PluginMethod
  public void status(PluginCall call) {
    eventTarget = this;
    EchoAudioPlugin existing = activeInstance;
    JSObject result = new JSObject();
    result.put("active", existing != null && existing.running && EchoCaptureService.isActive());
    result.put("foregroundService", EchoCaptureService.isActive());
    result.put("batteryOptimized", isBatteryOptimized());
    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(getContext());
    result.put("freeModeEnabled", runtime.isFreeModeEnabled());
    result.put("paused", runtime.isPaused());
    result.put("formalMode", runtime.isFormalMode());
    result.put("nativeUpload", runtime.hasUploadSession());
    result.put("queuedChunks", runtime.queuedCount());
    result.put("queuedBytes", runtime.queuedBytes());
    call.resolve(result);
  }

  @PluginMethod
  public void openBatteryOptimizationSettings(PluginCall call) {
    try {
      Intent intent = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
      intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
      getContext().startActivity(intent);
      call.resolve();
    } catch (Throwable error) {
      try {
        Intent fallback = new Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.parse("package:" + getContext().getPackageName())
        );
        fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(fallback);
        call.resolve();
      } catch (Throwable fallbackError) {
        call.reject(
            "battery optimization settings unavailable",
            fallbackError instanceof Exception
                ? (Exception) fallbackError
                : new Exception(fallbackError)
        );
      }
    }
  }

  @Override
  protected void handleOnDestroy() {
    if (eventTarget == this) {
      eventTarget = null;
    }
    if (!EchoCaptureService.isActive()) {
      stopActiveCapture();
    }
    super.handleOnDestroy();
  }

  static void stopActiveCaptureFromService() {
    EchoAudioPlugin existing = activeInstance;
    if (existing == null) return;
    EchoAudioPlugin target = eventTarget;
    synchronized (existing.lock) {
      existing.stopLocked();
    }
    if (target != null) {
      target.notifyListeners("stopped", new JSObject());
    }
    activeInstance = null;
    eventTarget = null;
  }

  static void pauseActiveCaptureFromService() {
    stopActiveCaptureFromService();
  }

  private void stopActiveCapture() {
    stopActiveCaptureFromService();
    EchoCaptureService.stop(getContext());
  }

  private boolean isBatteryOptimized() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return false;
    PowerManager manager =
        (PowerManager) getContext().getSystemService(android.content.Context.POWER_SERVICE);
    return manager != null
        && !manager.isIgnoringBatteryOptimizations(getContext().getPackageName());
  }

  private AudioRecord buildRecorder(int source, int sampleRate) {
    // RECORD_AUDIO can be revoked after Capacitor's permission callback but
    // before this recorder is constructed. Re-check at the exact privileged
    // boundary; the SecurityException catch below covers the remaining race.
    if (ContextCompat.checkSelfPermission(getContext(), Manifest.permission.RECORD_AUDIO)
        != PackageManager.PERMISSION_GRANTED) {
      Log.w(TAG, "RECORD_AUDIO was revoked before AudioRecord construction");
      return null;
    }
    int minBuffer = AudioRecord.getMinBufferSize(sampleRate, CHANNEL_CONFIG, AUDIO_FORMAT);
    if (minBuffer <= 0) {
      Log.w(TAG, "Invalid min buffer for " + sourceToName(source) + ": " + minBuffer);
      return null;
    }
    int bufferSize = Math.max(minBuffer * 4, sampleRate);
    try {
      AudioFormat format = new AudioFormat.Builder()
          .setEncoding(AUDIO_FORMAT)
          .setSampleRate(sampleRate)
          .setChannelMask(CHANNEL_CONFIG)
          .build();
      AudioRecord rec = new AudioRecord.Builder()
          .setAudioSource(source)
          .setAudioFormat(format)
          .setBufferSizeInBytes(bufferSize)
          .build();
      if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
        rec.release();
        return null;
      }
      return rec;
    } catch (SecurityException e) {
      Log.w(TAG, "RECORD_AUDIO was revoked while constructing AudioRecord", e);
      return null;
    } catch (Throwable t) {
      Log.w(TAG, "AudioRecord build failed for " + sourceToName(source), t);
      return null;
    }
  }

  private static AudioStats probeInput(AudioRecord rec, int sampleRate) {
    int probeBytes = Math.max(4096, sampleRate * 2 * PROBE_MS / 1000);
    byte[] probe = new byte[probeBytes];
    int offset = 0;
    long deadline = System.currentTimeMillis() + 1200;
    while (offset < probe.length && System.currentTimeMillis() < deadline) {
      int n = rec.read(probe, offset, probe.length - offset);
      if (n > 0) {
        offset += n;
      } else if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_DEAD_OBJECT) {
        break;
      }
    }
    return audioStats(probe, offset - (offset % 2));
  }

  private void recordLoop(int sampleRate, int chunkMs) {
    int bytesPerSample = 2;
    int chunkBytes = Math.max(sampleRate * bytesPerSample, sampleRate * bytesPerSample * chunkMs / 1000);
    byte[] readBuffer = new byte[Math.max(4096, sampleRate / 4 * bytesPerSample)];
    ByteArrayOutputStream chunk = new ByteArrayOutputStream(chunkBytes + 4096);

    while (running) {
      AudioRecord rec;
      synchronized (lock) {
        rec = recorder;
      }
      if (rec == null) break;

      int n = rec.read(readBuffer, 0, readBuffer.length);
      if (n > 0) {
        chunk.write(readBuffer, 0, n);
        if (chunk.size() >= chunkBytes) {
          emitChunk(chunk.toByteArray(), sampleRate);
          chunk.reset();
        }
      } else if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_DEAD_OBJECT) {
        emitError("AudioRecord read failed: " + n);
        break;
      }
    }

    if (chunk.size() > sampleRate * bytesPerSample / 2) {
      emitChunk(chunk.toByteArray(), sampleRate);
    }
    Log.i(TAG, "AudioRecord loop ended source=" + activeSource + " sampleRate=" + sampleRate);
  }

  private void emitChunk(byte[] pcm, int sampleRate) {
    try {
      AudioStats stats = audioStats(pcm);
      Log.i(
          TAG,
          "AudioRecord chunk source=" + activeSource
              + " sampleRate=" + sampleRate
              + " bytes=" + pcm.length
              + " rms=" + Math.round(stats.rms)
              + " peak=" + stats.peak
      );
      boolean nativeOwned =
          EchoCaptureRuntime
              .get(getContext())
              .acceptPcm(
                  pcm,
                  sampleRate,
                  activeSource,
                  stats.rms,
                  stats.peak
              );
      if (nativeOwned) {
        JSObject data = new JSObject();
        data.put("sampleRate", sampleRate);
        data.put("source", activeSource);
        data.put("rms", stats.rms);
        data.put("peak", stats.peak);
        data.put("nativeOwned", true);
        EchoAudioPlugin target = eventTarget;
        if (target != null) {
          target.notifyListeners("chunk", data);
        }
        return;
      }
      JSObject data = new JSObject();
      data.put("sampleRate", sampleRate);
      data.put("source", activeSource);
      data.put("rms", stats.rms);
      data.put("peak", stats.peak);
      data.put("base64", Base64.encodeToString(wavBytes(pcm, sampleRate), Base64.NO_WRAP));
      EchoAudioPlugin target = eventTarget;
      if (target != null) {
        target.notifyListeners("chunk", data);
      }
    } catch (IOException e) {
      emitError("Failed to build wav chunk: " + e.getMessage());
    }
  }

  private void emitError(String message) {
    Log.w(TAG, message);
    JSObject data = new JSObject();
    data.put("message", message);
    data.put("source", activeSource);
    EchoAudioPlugin target = eventTarget;
    if (target != null) {
      target.notifyListeners("error", data);
    }
  }

  private void stopLocked() {
    running = false;
    if (recorder != null) {
      try {
        recorder.stop();
      } catch (Throwable ignored) {
      }
      recorder.release();
      recorder = null;
    }
    worker = null;
  }

  private static String sourceToName(int source) {
    if (source == MediaRecorder.AudioSource.DEFAULT) return "DEFAULT";
    if (source == MediaRecorder.AudioSource.VOICE_RECOGNITION) return "VOICE_RECOGNITION";
    if (source == MediaRecorder.AudioSource.MIC) return "MIC";
    if (source == MediaRecorder.AudioSource.VOICE_COMMUNICATION) return "VOICE_COMMUNICATION";
    if (source == MediaRecorder.AudioSource.CAMCORDER) return "CAMCORDER";
    return String.valueOf(source);
  }

  private static AudioStats audioStats(byte[] pcm) {
    return audioStats(pcm, pcm.length);
  }

  private static AudioStats audioStats(byte[] pcm, int length) {
    long sumSquares = 0;
    int peak = 0;
    int safeLength = Math.max(0, Math.min(length, pcm.length));
    int samples = safeLength / 2;
    for (int i = 0; i + 1 < safeLength; i += 2) {
      int lo = pcm[i] & 0xff;
      int hi = pcm[i + 1];
      int v = (short) ((hi << 8) | lo);
      int abs = Math.abs(v);
      if (abs > peak) peak = abs;
      sumSquares += (long) v * (long) v;
    }
    double rms = samples > 0 ? Math.sqrt((double) sumSquares / (double) samples) : 0.0;
    return new AudioStats(rms, peak);
  }

  private static boolean isDeadInput(AudioStats stats) {
    return stats.rms <= DEAD_INPUT_RMS_FLOOR && stats.peak <= DEAD_INPUT_PEAK_FLOOR;
  }

  private static final class AudioStats {
    final double rms;
    final int peak;

    AudioStats(double rms, int peak) {
      this.rms = rms;
      this.peak = peak;
    }
  }

  static byte[] wavBytesForNativeQueue(byte[] pcm, int sampleRate) throws IOException {
    return wavBytes(pcm, sampleRate);
  }

  private static byte[] wavBytes(byte[] pcm, int sampleRate) throws IOException {
    ByteArrayOutputStream out = new ByteArrayOutputStream(pcm.length + 44);
    int byteRate = sampleRate * 2;
    int dataLen = pcm.length;
    out.write(new byte[] {'R', 'I', 'F', 'F'});
    writeLeInt(out, 36 + dataLen);
    out.write(new byte[] {'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
    writeLeInt(out, 16);
    writeLeShort(out, (short) 1);
    writeLeShort(out, (short) 1);
    writeLeInt(out, sampleRate);
    writeLeInt(out, byteRate);
    writeLeShort(out, (short) 2);
    writeLeShort(out, (short) 16);
    out.write(new byte[] {'d', 'a', 't', 'a'});
    writeLeInt(out, dataLen);
    out.write(pcm);
    return out.toByteArray();
  }

  private static void writeLeInt(ByteArrayOutputStream out, int value) throws IOException {
    out.write(ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(value).array());
  }

  private static void writeLeShort(ByteArrayOutputStream out, short value) throws IOException {
    out.write(ByteBuffer.allocate(2).order(ByteOrder.LITTLE_ENDIAN).putShort(value).array());
  }

  private static JSObject runtimeStatus(EchoCaptureRuntime runtime) {
    JSObject result = new JSObject();
    result.put("freeModeEnabled", runtime.isFreeModeEnabled());
    result.put("paused", runtime.isPaused());
    result.put("formalMode", runtime.isFormalMode());
    result.put("nativeUpload", runtime.hasUploadSession());
    result.put("queuedChunks", runtime.queuedCount());
    result.put("queuedBytes", runtime.queuedBytes());
    return result;
  }
}

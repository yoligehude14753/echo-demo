package com.echodesk.app;

import android.Manifest;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.util.Base64;
import android.util.Log;

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
        @Permission(alias = "microphone", strings = {Manifest.permission.RECORD_AUDIO})
    }
)
public class EchoAudioPlugin extends Plugin {
  private static final String TAG = "EchoDeskAudio";
  private static final int DEFAULT_SAMPLE_RATE = 16000;
  private static final int DEFAULT_CHUNK_MS = 6000;
  private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO;
  private static final int AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT;
  private static final int PROBE_MS = 500;
  private static final int[] AUDIO_SOURCES = {
      MediaRecorder.AudioSource.MIC,
      MediaRecorder.AudioSource.VOICE_RECOGNITION,
      MediaRecorder.AudioSource.DEFAULT,
      MediaRecorder.AudioSource.VOICE_COMMUNICATION,
      MediaRecorder.AudioSource.CAMCORDER
  };

  private final Object lock = new Object();
  private AudioRecord recorder = null;
  private Thread worker = null;
  private volatile boolean running = false;
  private int activeSampleRate = DEFAULT_SAMPLE_RATE;
  private String activeSource = "unknown";

  @PluginMethod
  public void start(PluginCall call) {
    if (getPermissionState("microphone") != PermissionState.GRANTED) {
      requestPermissionForAlias("microphone", call, "startAfterPermission");
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
    startRecording(call);
  }

  private void startRecording(PluginCall call) {
    int sampleRate = call.getInt("sampleRate", DEFAULT_SAMPLE_RATE);
    int chunkMs = call.getInt("chunkMs", DEFAULT_CHUNK_MS);
    if (sampleRate <= 0) sampleRate = DEFAULT_SAMPLE_RATE;
    if (chunkMs < 1000) chunkMs = DEFAULT_CHUNK_MS;

    synchronized (lock) {
      stopLocked();
      AudioRecord next = null;
      String sourceName = "unknown";
      int selectedSampleRate = sampleRate;
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
            if (probeStats.peak > 0) {
              sourceName = sourceToName(source);
              selectedSampleRate = candidateRate;
              break;
            }
            try {
              next.stop();
            } catch (Throwable ignored) {
            }
            next.release();
            next = null;
            continue;
          }
          next.release();
          next = null;
        }
        if (next != null) break;
      }

      if (next == null) {
        call.reject("Android AudioRecord opened microphone sources, but every source returned silent PCM. Please connect a USB/Bluetooth conference microphone or enable TV microphone access.");
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
    }

    JSObject result = new JSObject();
    result.put("sampleRate", activeSampleRate);
    result.put("source", activeSource);
    call.resolve(result);
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
    synchronized (lock) {
      stopLocked();
    }
    call.resolve();
  }

  @Override
  protected void handleOnDestroy() {
    synchronized (lock) {
      stopLocked();
    }
    super.handleOnDestroy();
  }

  private AudioRecord buildRecorder(int source, int sampleRate) {
    int minBuffer = AudioRecord.getMinBufferSize(sampleRate, CHANNEL_CONFIG, AUDIO_FORMAT);
    if (minBuffer <= 0) {
      Log.w(TAG, "Invalid min buffer for " + sourceToName(source) + ": " + minBuffer);
      return null;
    }
    int bufferSize = Math.max(minBuffer * 4, sampleRate);
    try {
      AudioRecord rec;
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
        AudioFormat format = new AudioFormat.Builder()
            .setEncoding(AUDIO_FORMAT)
            .setSampleRate(sampleRate)
            .setChannelMask(CHANNEL_CONFIG)
            .build();
        rec = new AudioRecord.Builder()
            .setAudioSource(source)
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufferSize)
            .build();
      } else {
        rec = new AudioRecord(source, sampleRate, CHANNEL_CONFIG, AUDIO_FORMAT, bufferSize);
      }
      if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
        rec.release();
        return null;
      }
      return rec;
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
      JSObject data = new JSObject();
      data.put("sampleRate", sampleRate);
      data.put("source", activeSource);
      data.put("rms", stats.rms);
      data.put("peak", stats.peak);
      data.put("base64", Base64.encodeToString(wavBytes(pcm, sampleRate), Base64.NO_WRAP));
      notifyListeners("chunk", data);
    } catch (IOException e) {
      emitError("Failed to build wav chunk: " + e.getMessage());
    }
  }

  private void emitError(String message) {
    Log.w(TAG, message);
    JSObject data = new JSObject();
    data.put("message", message);
    data.put("source", activeSource);
    notifyListeners("error", data);
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

  private static final class AudioStats {
    final double rms;
    final int peak;

    AudioStats(double rms, int peak) {
      this.rms = rms;
      this.peak = peak;
    }
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
}

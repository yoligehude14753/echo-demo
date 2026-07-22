package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.os.Bundle;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.Arrays;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Task-owned native uploader acceptance fixture.
 *
 * This test is instrumentation-only: it calls the package-private runtime
 * directly, injects no HTTP substitute, and receives the real URL/token only
 * through instrumentation arguments.
 */
@RunWith(AndroidJUnit4.class)
public class EchoCaptureRuntimeAcceptanceTest {
  private static final String ASSET = "controlled-zh-16k-mono.wav";
  private static final long DRAIN_TIMEOUT_MS = 60_000L;

  @Test
  public void controlledPcmReachesTheRealNativeUploader() throws Exception {
    Bundle args = InstrumentationRegistry.getArguments();
    String baseUrl = requiredArg(args, "echodesk.baseUrl");
    String bearerToken = requiredArg(args, "echodesk.bearerToken");
    String deviceId = requiredArg(args, "echodesk.deviceId");
    byte[] pcm = readPcm16MonoWav();

    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(
        InstrumentationRegistry.getInstrumentation().getTargetContext()
    );
    assertEquals("acceptance must start with an empty task-owned queue", 0, runtime.queuedCount());
    runtime.setPaused(false);
    runtime.markFreeModeEnabled(true);
    runtime.setFormalMode(false, "");
    runtime.configureSession(baseUrl, bearerToken, deviceId);

    assertTrue(runtime.acceptPcm(pcm, 16_000, "androidTest", 0.0, 0));
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(DRAIN_TIMEOUT_MS);
    while (runtime.queuedCount() > 0 && System.nanoTime() < deadline) {
      Thread.sleep(250L);
    }
    assertEquals("native uploader retained a record; inspect native_capture_result log", 0, runtime.queuedCount());
    runtime.clearSession();
  }

  private static String requiredArg(Bundle args, String key) {
    String value = args.getString(key, "").trim();
    if (value.isEmpty()) throw new AssertionError("missing instrumentation argument: " + key);
    return value;
  }

  private static byte[] readPcm16MonoWav() throws Exception {
    try (InputStream input = InstrumentationRegistry.getInstrumentation().getContext()
        .getAssets().open(ASSET)) {
      ByteArrayOutputStream output = new ByteArrayOutputStream();
      byte[] buffer = new byte[8 * 1024];
      int count;
      while ((count = input.read(buffer)) >= 0) {
        if (count > 0) output.write(buffer, 0, count);
      }
      byte[] wav = output.toByteArray();
      if (wav.length < 44 || wav[0] != 'R' || wav[1] != 'I' || wav[2] != 'F' || wav[3] != 'F') {
        throw new AssertionError("controlled asset is not a RIFF WAV");
      }
      for (int offset = 12; offset + 8 <= wav.length; ) {
        int chunkSize = littleEndianInt(wav, offset + 4);
        if (wav[offset] == 'd' && wav[offset + 1] == 'a' && wav[offset + 2] == 't' && wav[offset + 3] == 'a') {
          int start = offset + 8;
          int end = Math.min(wav.length, start + chunkSize);
          return Arrays.copyOfRange(wav, start, end);
        }
        offset += 8 + chunkSize + (chunkSize & 1);
      }
      throw new AssertionError("controlled WAV has no data chunk");
    }
  }

  private static int littleEndianInt(byte[] bytes, int offset) {
    return (bytes[offset] & 0xff)
        | ((bytes[offset + 1] & 0xff) << 8)
        | ((bytes[offset + 2] & 0xff) << 16)
        | ((bytes[offset + 3] & 0xff) << 24);
  }
}

package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * Task-owned native uploader acceptance fixture.
 *
 * This test starts the real renderer bootstrap path, consumes the debug-only
 * app-private handoff, then calls the package-private runtime directly. The
 * default assertion stops after native session acquisition; an explicit
 * task-owned runUpload=true additionally exercises the real uploader. No HTTP
 * substitute or bearer instrumentation argument is accepted.
 */
@RunWith(AndroidJUnit4.class)
public class EchoCaptureRuntimeAcceptanceTest {
  private static final String ASSET = "controlled-zh-16k-mono.wav";
  private static final long HANDOFF_TIMEOUT_MS = 45_000L;
  private static final long QUEUE_SNAPSHOT_TIMEOUT_MS = 10_000L;
  private static final long DRAIN_TIMEOUT_MS = 60_000L;
  private static final long AGENT_ARTIFACT_TIMEOUT_MS = 120_000L;
  private static final int MAX_HTTP_BODY_BYTES = 256 * 1024;

  @Test
  public void controlledPcmReachesTheRealNativeUploader() throws Exception {
    android.app.Instrumentation instrumentation =
        InstrumentationRegistry.getInstrumentation();
    android.content.Context targetContext = instrumentation.getTargetContext();
    SessionHandoff.clear(targetContext);
    launchProductBootstrap(instrumentation, targetContext);
    SessionHandoff.Credentials credentials = waitForSessionHandoff(
        targetContext,
        HANDOFF_TIMEOUT_MS
    );
    byte[] pcm = readPcm16MonoWav();

    EchoCaptureRuntime runtime = EchoCaptureRuntime.get(targetContext);
    assertEquals("acceptance must start with an empty task-owned queue", 0, runtime.queuedCount());
    runtime.setPaused(false);
    runtime.markFreeModeEnabled(true);
    runtime.setFormalMode(false, "");
    runtime.configureSession(
        credentials.baseUrl,
        credentials.bearerToken,
        credentials.deviceId
    );
    assertTrue(
        "product handoff must provide a complete native upload session",
        runtime.hasUploadSession()
    );
    if (!Boolean.parseBoolean(
        InstrumentationRegistry.getArguments().getString("echodesk.runUpload", "false")
    )) {
      runtime.clearSession();
      return;
    }

    assertTrue(runtime.acceptPcm(pcm, 16_000, "androidTest", 0.0, 0));
    NativeCaptureQueue queue = new NativeCaptureQueue(
        new File(targetContext.getFilesDir(), "native-capture-queue")
    );
    String segmentId = waitForQueuedSegmentId(queue, QUEUE_SNAPSHOT_TIMEOUT_MS);
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(DRAIN_TIMEOUT_MS);
    while (runtime.queuedCount() > 0 && System.nanoTime() < deadline) {
      Thread.sleep(250L);
    }
    assertEquals("native uploader retained a record; inspect native_capture_result log", 0, runtime.queuedCount());
    assertAuthenticatedRecentAndAgentArtifact(credentials, segmentId, targetContext);
    runtime.clearSession();
  }

  private static String waitForQueuedSegmentId(
      NativeCaptureQueue queue,
      long timeoutMs
  ) throws Exception {
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
    while (System.nanoTime() < deadline) {
      java.util.List<NativeCaptureQueue.Record> records = queue.records();
      if (!records.isEmpty()) return records.get(records.size() - 1).id();
      Thread.sleep(25L);
    }
    throw new AssertionError("native upload did not expose an app-private queued segment id");
  }

  private static void assertAuthenticatedRecentAndAgentArtifact(
      SessionHandoff.Credentials credentials,
      String segmentId,
      android.content.Context targetContext
  ) throws Exception {
    HttpResult recent = request(
        credentials,
        "GET",
        "/capture/recent?limit=100",
        null,
        null,
        targetContext
    );
    assertEquals("authenticated recent readback failed", 200, recent.status);
    JSONArray segments = new JSONArray(recent.body);
    JSONObject matching = null;
    for (int index = 0; index < segments.length(); index++) {
      JSONObject candidate = segments.optJSONObject(index);
      if (candidate != null && segmentId.equals(candidate.optString("segment_id", ""))) {
        matching = candidate;
        break;
      }
    }
    assertTrue("authenticated recent did not contain the exact native segment", matching != null);
    String text = matching.optString("text", "").trim();
    assertTrue("authenticated recent returned an empty native transcript", !text.isEmpty());

    JSONObject artifactRequest = new JSONObject()
        .put("artifact_type", "markdown")
        .put("brief", "请把这段真实收音转写整理成一份简短 Markdown 纪要：" + text)
        .put("extra_instructions", "只输出可读的要点和一句结论，不要提及测试、token 或内部接口。");
    HttpResult generated = request(
        credentials,
        "POST",
        "/artifacts/generate",
        artifactRequest.toString(),
        "application/json",
        targetContext
    );
    assertEquals("真实 Agent artifact 生成被 provider/runtime 拒绝", 200, generated.status);
    JSONObject artifact = new JSONObject(generated.body);
    String artifactId = artifact.optString("artifact_id", "").trim();
    assertTrue("真实 Agent artifact response omitted artifact_id", !artifactId.isEmpty());
    assertTrue(
        "真实 Agent artifact id was not a safe backend-generated identifier",
        artifactId.matches("[A-Za-z0-9._-]{1,160}")
    );
    HttpResult downloaded = request(
        credentials,
        "GET",
        "/artifacts/" + artifactId + "/download",
        null,
        null,
        targetContext
    );
    assertEquals("真实 Agent artifact download failed", 200, downloaded.status);
    assertTrue("真实 Agent artifact download was empty", downloaded.bodyBytes > 0);
  }

  private static HttpResult request(
      SessionHandoff.Credentials credentials,
      String method,
      String path,
      String requestBody,
      String contentType,
      android.content.Context targetContext
  ) throws Exception {
    String baseUrl = credentials.baseUrl.trim();
    while (baseUrl.endsWith("/")) baseUrl = baseUrl.substring(0, baseUrl.length() - 1);
    URL endpoint = new URL(baseUrl + path);
    HttpURLConnection connection = (HttpURLConnection) endpoint.openConnection();
    connection.setRequestMethod(method);
    connection.setConnectTimeout(15_000);
    connection.setReadTimeout((int) AGENT_ARTIFACT_TIMEOUT_MS);
    connection.setInstanceFollowRedirects(false);
    connection.setRequestProperty("Authorization", "Bearer " + credentials.bearerToken);
    connection.setRequestProperty("X-EchoDesk-Client-Version", appVersion(targetContext));
    if (requestBody != null) {
      connection.setDoOutput(true);
      connection.setRequestProperty("Content-Type", contentType);
      byte[] bytes = requestBody.getBytes(StandardCharsets.UTF_8);
      try (OutputStream output = connection.getOutputStream()) {
        output.write(bytes);
      }
    }
    int status = connection.getResponseCode();
    long bodyBytes = 0;
    String body = "";
    try (InputStream input = status >= 400
        ? connection.getErrorStream()
        : connection.getInputStream()) {
      if (input != null) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8 * 1024];
        int count;
        while ((count = input.read(buffer)) >= 0) {
          if (count == 0) continue;
          bodyBytes += count;
          if (output.size() < MAX_HTTP_BODY_BYTES) {
            output.write(buffer, 0, Math.min(count, MAX_HTTP_BODY_BYTES - output.size()));
          }
        }
        body = output.toString(StandardCharsets.UTF_8.name());
      }
    } finally {
      connection.disconnect();
    }
    return new HttpResult(status, body, bodyBytes);
  }

  private static String appVersion(android.content.Context context) {
    try {
      PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
      return info.versionName == null ? "unknown" : info.versionName;
    } catch (Exception ignored) {
      return "unknown";
    }
  }

  private static final class HttpResult {
    final int status;
    final String body;
    final long bodyBytes;

    HttpResult(int status, String body, long bodyBytes) {
      this.status = status;
      this.body = body;
      this.bodyBytes = bodyBytes;
    }
  }

  private static void launchProductBootstrap(
      android.app.Instrumentation instrumentation,
      android.content.Context targetContext
  ) throws Exception {
    Intent intent = new Intent(targetContext, MainActivity.class)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    Activity activity = instrumentation.startActivitySync(intent);
    WebView webView = null;
    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
    while (System.nanoTime() < deadline) {
      WebView[] candidate = new WebView[1];
      instrumentation.runOnMainSync(() -> {
        WebView found = findWebView(activity.getWindow().getDecorView());
        if (found != null && found.getUrl() != null) candidate[0] = found;
      });
      webView = candidate[0];
      if (webView != null) break;
      Thread.sleep(250L);
    }
    if (webView == null) {
      throw new AssertionError("MainActivity WebView did not load");
    }
    final WebView loadedWebView = webView;
    instrumentation.runOnMainSync(() -> loadedWebView.evaluateJavascript(
        "(function(){localStorage.setItem('echodesk.onboarding.completed','1');return 'ok';})()",
        null
    ));
    instrumentation.runOnMainSync(loadedWebView::reload);
  }

  private static SessionHandoff.Credentials waitForSessionHandoff(
      android.content.Context targetContext,
      long timeoutMs
  ) throws Exception {
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs);
    while (System.nanoTime() < deadline) {
      SessionHandoff.Credentials credentials = SessionHandoff.consume(targetContext);
      if (credentials != null) return credentials;
      Thread.sleep(250L);
    }
    throw new AssertionError(
        "product bootstrap did not publish a short-lived app-private native session handoff"
    );
  }

  private static WebView findWebView(View root) {
    if (root instanceof WebView) return (WebView) root;
    if (!(root instanceof ViewGroup)) return null;
    ViewGroup group = (ViewGroup) root;
    for (int index = 0; index < group.getChildCount(); index++) {
      WebView found = findWebView(group.getChildAt(index));
      if (found != null) return found;
    }
    return null;
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

package com.echodesk.app;

import android.content.Intent;
import android.net.Uri;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.UUID;

@CapacitorPlugin(name = "EchoUpdater")
public final class EchoUpdaterPlugin extends Plugin {
  private static final long MAX_APK_BYTES = 1024L * 1024L * 1024L;

  @PluginMethod
  public void downloadAndInstall(PluginCall call) {
    String rawUrl = call.getString("url");
    String rawDigest = call.getString("digest");
    Long expectedSize = call.getLong("size");
    if (
      rawUrl == null ||
      rawDigest == null ||
      expectedSize == null ||
      expectedSize < 1 ||
      expectedSize > MAX_APK_BYTES ||
      !rawDigest.matches("(?i)^sha256:[0-9a-f]{64}$")
    ) {
      call.reject("invalid update contract", "UPDATE_CONTRACT_INVALID");
      return;
    }
    new Thread(() -> {
      File target = null;
      try {
        File updateRoot = new File(getContext().getCacheDir(), "echodesk-updates");
        if (!updateRoot.exists() && !updateRoot.mkdirs()) {
          throw new IllegalStateException("update cache unavailable");
        }
        target = new File(updateRoot, "EchoDesk-" + UUID.randomUUID() + ".apk");
        downloadVerified(
          new URL(rawUrl),
          rawDigest.substring("sha256:".length()).toLowerCase(Locale.ROOT),
          expectedSize,
          target
        );
        Uri uri = FileProvider.getUriForFile(
          getContext(),
          getContext().getPackageName() + ".fileprovider",
          target
        );
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(uri, "application/vnd.android.package-archive");
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        JSObject result = new JSObject();
        result.put("ok", true);
        result.put("requiresUserConfirmation", true);
        File finalTarget = target;
        getActivity().runOnUiThread(() -> {
          try {
            getContext().startActivity(intent);
            call.resolve(result);
          } catch (Exception error) {
            finalTarget.delete();
            call.reject("package installer unavailable", "UPDATE_INSTALLER_UNAVAILABLE");
          }
        });
      } catch (Exception error) {
        if (target != null) target.delete();
        call.reject("update download or verification failed", "UPDATE_VERIFY_FAILED");
      }
    }, "echodesk-update-download").start();
  }

  private static void downloadVerified(
    URL initial,
    String expectedDigest,
    long expectedSize,
    File target
  ) throws Exception {
    URL current = initial;
    for (int redirects = 0; redirects <= 5; redirects += 1) {
      requireGithubUrl(current);
      HttpURLConnection connection = (HttpURLConnection) current.openConnection();
      connection.setInstanceFollowRedirects(false);
      connection.setConnectTimeout(10_000);
      connection.setReadTimeout(30_000);
      connection.setRequestProperty("Accept", "application/octet-stream");
      connection.setRequestProperty("User-Agent", "EchoDesk-Android-Updater");
      int status = connection.getResponseCode();
      if (status >= 300 && status < 400) {
        String location = connection.getHeaderField("Location");
        connection.disconnect();
        if (location == null) throw new IllegalStateException("redirect missing");
        current = new URL(current, location);
        continue;
      }
      if (status < 200 || status >= 300) {
        connection.disconnect();
        throw new IllegalStateException("download failed");
      }
      long declared = connection.getContentLengthLong();
      if (declared > 0 && declared != expectedSize) {
        connection.disconnect();
        throw new IllegalStateException("size mismatch");
      }
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      long received = 0;
      try (
        InputStream input = connection.getInputStream();
        FileOutputStream output = new FileOutputStream(target, false)
      ) {
        byte[] buffer = new byte[64 * 1024];
        int count;
        while ((count = input.read(buffer)) != -1) {
          received += count;
          if (received > expectedSize || received > MAX_APK_BYTES) {
            throw new IllegalStateException("size limit exceeded");
          }
          digest.update(buffer, 0, count);
          output.write(buffer, 0, count);
        }
        output.getFD().sync();
      } finally {
        connection.disconnect();
      }
      StringBuilder actual = new StringBuilder(64);
      for (byte value : digest.digest()) {
        actual.append(String.format(Locale.ROOT, "%02x", value & 0xff));
      }
      if (received != expectedSize || !actual.toString().equals(expectedDigest)) {
        throw new IllegalStateException("digest mismatch");
      }
      return;
    }
    throw new IllegalStateException("redirect limit exceeded");
  }

  private static void requireGithubUrl(URL url) {
    String host = url.getHost().toLowerCase(Locale.ROOT);
    if (
      !"https".equals(url.getProtocol()) ||
      !(
        host.equals("github.com") ||
        host.equals("api.github.com") ||
        host.endsWith(".githubusercontent.com")
      )
    ) {
      throw new IllegalArgumentException("update URL is not an allowed GitHub endpoint");
    }
  }
}

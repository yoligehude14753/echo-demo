package com.echodesk.app;

import android.content.Context;

/**
 * Debug-only, process-memory bridge for task-owned instrumentation.
 *
 * <p>The renderer already passes the short-lived session to the native
 * Capacitor plugin. This test-only bridge exposes the same process memory to
 * instrumentation without writing a bearer, device id, or enrollment value
 * to cache/files. Release keeps the no-op implementation.</p>
 */
final class SessionHandoff {
  private static final long TTL_MS = 60_000L;
  private static final Object LOCK = new Object();
  private static Credentials pending;
  private static long pendingExpiresAt;

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
    synchronized (LOCK) {
      pending = new Credentials(normalizedBaseUrl, normalizedBearerToken, normalizedDeviceId);
      pendingExpiresAt = System.currentTimeMillis() + TTL_MS;
    }
  }

  static Credentials consume(Context context) {
    synchronized (LOCK) {
      Credentials claimed = pending;
      if (claimed == null || pendingExpiresAt <= System.currentTimeMillis()) {
        pending = null;
        pendingExpiresAt = 0L;
        return null;
      }
      pending = null;
      pendingExpiresAt = 0L;
      return claimed;
    }
  }

  static void clear(Context context) {
    synchronized (LOCK) {
      pending = null;
      pendingExpiresAt = 0L;
    }
  }

  private static String normalize(String value) {
    return value == null ? "" : value.trim();
  }
}

package com.echodesk.app;

import android.content.Context;

/** Release keeps the debug instrumentation bridge out of the product surface. */
final class SessionHandoff {
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

  static void publish(Context context, String baseUrl, String bearerToken, String deviceId) {}
}

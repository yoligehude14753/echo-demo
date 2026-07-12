package com.echodesk.app;

import java.net.IDN;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;

/** Backend origin canonicalization shared by the native identity vault and tests. */
final class IdentityOrigin {
  private IdentityOrigin() {}

  static String normalize(String raw) {
    if (raw == null || raw.trim().isEmpty()) {
      throw new IllegalArgumentException("backend origin is required");
    }

    final URI uri;
    try {
      uri = new URI(raw.trim());
    } catch (URISyntaxException error) {
      throw new IllegalArgumentException("backend origin is invalid", error);
    }

    String scheme = uri.getScheme();
    String host = uri.getHost();
    if (scheme == null || host == null) {
      throw new IllegalArgumentException("backend origin must be an absolute HTTPS URL");
    }
    scheme = scheme.toLowerCase(Locale.ROOT);
    if (!"https".equals(scheme)) {
      throw new IllegalArgumentException("identity credentials require HTTPS");
    }
    if (uri.getUserInfo() != null) {
      throw new IllegalArgumentException("backend origin must not contain user info");
    }

    host = IDN.toASCII(host.toLowerCase(Locale.ROOT));
    if (host.indexOf(':') >= 0 && !host.startsWith("[")) {
      host = "[" + host + "]";
    }
    int port = uri.getPort();
    if (port == 443) {
      port = -1;
    }
    return scheme + "://" + host + (port >= 0 ? ":" + port : "");
  }

  static String storageKey(String normalizedOrigin) {
    try {
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      byte[] hash = digest.digest(normalizedOrigin.getBytes(StandardCharsets.UTF_8));
      StringBuilder value = new StringBuilder("identity.");
      for (byte part : hash) {
        value.append(String.format(Locale.ROOT, "%02x", part & 0xff));
      }
      return value.toString();
    } catch (NoSuchAlgorithmException impossible) {
      throw new IllegalStateException("SHA-256 unavailable", impossible);
    }
  }
}

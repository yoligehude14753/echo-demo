package com.echodesk.app;

import java.security.SecureRandom;
import java.util.HashMap;
import java.util.Map;
import java.util.Base64;

/**
 * Process-only Android public identity store.
 *
 * <p>Public device and enrollment credentials deliberately have no durable
 * Android backing store. A process restart drops this map and the renderer
 * must bootstrap a new identity. The static map only keeps activity/plugin
 * recreation in the same process from creating competing identities.</p>
 */
final class AndroidMemoryIdentityStore {
  private static final int SECRET_BYTES = 32;
  private static final Object PROCESS_LOCK = new Object();
  private static final Map<String, StoredIdentity> IDENTITIES = new HashMap<>();

  private final SecureRandom random = new SecureRandom();

  static final class IdentityMaterial {
    final String origin;
    final String enrollmentId;
    final String deviceSecret;
    final boolean created;
    final boolean enrollmentConfirmed;
    final String pendingRotationId;
    final String pendingDeviceSecret;

    IdentityMaterial(
        String origin,
        String enrollmentId,
        String deviceSecret,
        boolean created,
        boolean enrollmentConfirmed,
        String pendingRotationId,
        String pendingDeviceSecret
    ) {
      this.origin = origin;
      this.enrollmentId = enrollmentId;
      this.deviceSecret = deviceSecret;
      this.created = created;
      this.enrollmentConfirmed = enrollmentConfirmed;
      this.pendingRotationId = pendingRotationId;
      this.pendingDeviceSecret = pendingDeviceSecret;
    }
  }

  static final class RotationMaterial {
    final String origin;
    final String rotationId;
    final String currentDeviceSecret;
    final String newDeviceSecret;

    RotationMaterial(
        String origin,
        String rotationId,
        String currentDeviceSecret,
        String newDeviceSecret
    ) {
      this.origin = origin;
      this.rotationId = rotationId;
      this.currentDeviceSecret = currentDeviceSecret;
      this.newDeviceSecret = newDeviceSecret;
    }
  }

  static final class IdentityLostException extends Exception {
    IdentityLostException() {
      super("identity_lost");
    }
  }

  static final class IdentityMissingException extends Exception {
    IdentityMissingException() {
      super("identity_missing");
    }
  }

  static final class StoreException extends Exception {
    StoreException(String message) {
      super(message);
    }
  }

  private static final class StoredIdentity {
    String enrollmentId;
    String deviceSecret;
    boolean enrollmentConfirmed;
    boolean lost;
    String pendingRotationId;
    String pendingDeviceSecret;
  }

  IdentityMaterial loadOrCreate(String rawOrigin)
      throws IdentityLostException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = IDENTITIES.get(origin);
      if (identity == null) {
        identity = new StoredIdentity();
        identity.enrollmentId = newSecret();
        identity.deviceSecret = newSecret();
        IDENTITIES.put(origin, identity);
        return material(origin, identity, true);
      }
      assertActive(identity);
      return material(origin, identity, false);
    }
  }

  IdentityMaterial loadForReconnect(String rawOrigin)
      throws IdentityMissingException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = IDENTITIES.get(origin);
      if (identity == null) throw new IdentityMissingException();
      return material(origin, identity, false);
    }
  }

  void confirmEnrollment(String rawOrigin)
      throws IdentityLostException, IdentityMissingException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      assertActive(identity);
      identity.enrollmentConfirmed = true;
    }
  }

  RotationMaterial beginRotation(String rawOrigin)
      throws IdentityLostException, IdentityMissingException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      assertActive(identity);
      if (identity.pendingRotationId == null || identity.pendingDeviceSecret == null) {
        identity.pendingRotationId = newSecret();
        identity.pendingDeviceSecret = newSecret();
      }
      return new RotationMaterial(
          origin,
          identity.pendingRotationId,
          identity.deviceSecret,
          identity.pendingDeviceSecret
      );
    }
  }

  void commitRotation(String rawOrigin, String rotationId)
      throws IdentityLostException, IdentityMissingException, StoreException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      assertActive(identity);
      assertRotation(identity, rotationId);
      identity.deviceSecret = identity.pendingDeviceSecret;
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
    }
  }

  void abortRotation(String rawOrigin, String rotationId)
      throws IdentityLostException, IdentityMissingException, StoreException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      assertActive(identity);
      assertRotation(identity, rotationId);
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
    }
  }

  void markIdentityLost(String rawOrigin) throws IdentityMissingException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      identity.lost = true;
    }
  }

  void restoreIdentity(String rawOrigin) throws IdentityMissingException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      StoredIdentity identity = requireExisting(origin);
      identity.lost = false;
    }
  }

  void clear(String rawOrigin) {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      IDENTITIES.remove(origin);
    }
  }

  static void clearProcessForTest() {
    synchronized (PROCESS_LOCK) {
      IDENTITIES.clear();
    }
  }

  private StoredIdentity requireExisting(String origin) throws IdentityMissingException {
    StoredIdentity identity = IDENTITIES.get(origin);
    if (identity == null) throw new IdentityMissingException();
    return identity;
  }

  private String newSecret() {
    byte[] value = new byte[SECRET_BYTES];
    random.nextBytes(value);
    return Base64.getUrlEncoder().withoutPadding().encodeToString(value);
  }

  private static IdentityMaterial material(
      String origin,
      StoredIdentity identity,
      boolean created
  ) {
    return new IdentityMaterial(
        origin,
        identity.enrollmentId,
        identity.deviceSecret,
        created,
        identity.enrollmentConfirmed,
        identity.pendingRotationId,
        identity.pendingDeviceSecret
    );
  }

  private static void assertActive(StoredIdentity identity)
      throws IdentityLostException {
    if (identity.lost) throw new IdentityLostException();
  }

  private static void assertRotation(StoredIdentity identity, String rotationId)
      throws StoreException {
    if (rotationId == null
        || identity.pendingRotationId == null
        || identity.pendingDeviceSecret == null
        || !identity.pendingRotationId.equals(rotationId)) {
      throw new StoreException("memory_identity_rotation_mismatch");
    }
  }
}

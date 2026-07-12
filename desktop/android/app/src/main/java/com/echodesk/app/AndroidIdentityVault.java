package com.echodesk.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyInfo;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONException;
import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Installation-local identity material encrypted by a non-exportable Android Keystore key.
 *
 * <p>Every record is keyed and authenticated by its canonical backend origin. SharedPreferences
 * contains only AES-GCM ciphertext. A failed decrypt or an identity-lost marker is fail-closed and
 * never replaced with a new owner implicitly.</p>
 */
final class AndroidIdentityVault {
  static final String PREFERENCES_NAME = "echodesk.identity.v1";

  private static final String KEYSTORE_PROVIDER = "AndroidKeyStore";
  private static final String KEY_ALIAS = "com.echodesk.app.identity.aes-gcm.v1";
  private static final String CIPHER = "AES/GCM/NoPadding";
  private static final int KEY_BITS = 256;
  private static final int IV_BYTES = 12;
  private static final int GCM_TAG_BITS = 128;
  private static final int SECRET_BYTES = 32;
  private static final int BLOB_VERSION = 1;
  private static final int RECORD_VERSION = 1;

  /**
   * SharedPreferences and the Keystore alias are process-wide. Keep every read-modify-write
   * sequence under the same process lock even when Capacitor or a test recreates the vault.
   */
  private static final Object PROCESS_LOCK = new Object();
  private final SharedPreferences preferences;
  private final SecureRandom random = new SecureRandom();

  AndroidIdentityVault(Context context) {
    preferences = context.getApplicationContext().getSharedPreferences(
        PREFERENCES_NAME,
        Context.MODE_PRIVATE
    );
  }

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

  static final class VaultException extends Exception {
    VaultException(String message) {
      super(message);
    }

    VaultException(String message, Throwable cause) {
      super(message, cause);
    }
  }

  private static final class StoredIdentity {
    String enrollmentId;
    String deviceSecret;
    boolean enrollmentConfirmed;
    boolean lost;
    long createdAt;
    long updatedAt;
    String pendingRotationId;
    String pendingDeviceSecret;
  }

  IdentityMaterial loadOrCreate(String rawOrigin)
      throws IdentityLostException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      String ciphertext = preferences.getString(storageKey, null);
      if (ciphertext == null) {
        long now = System.currentTimeMillis();
        StoredIdentity identity = new StoredIdentity();
        identity.enrollmentId = newSecret();
        identity.deviceSecret = newSecret();
        identity.enrollmentConfirmed = false;
        identity.createdAt = now;
        identity.updatedAt = now;
        write(origin, storageKey, identity);
        return material(origin, identity, true);
      }

      StoredIdentity identity = decryptRecord(origin, ciphertext);
      assertActive(identity);
      return material(origin, identity, false);
    }
  }

  /**
   * Explicit recovery read for a user-initiated reconnect. This never creates or silently
   * reactivates an owner; the lost marker remains set until restoreIdentity is called after the
   * backend accepts the exact stored credential.
   */
  IdentityMaterial loadForReconnect(String rawOrigin)
      throws IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      return material(origin, identity, false);
    }
  }

  void confirmEnrollment(String rawOrigin)
      throws IdentityLostException, IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      assertActive(identity);
      if (identity.enrollmentConfirmed) {
        return;
      }
      identity.enrollmentConfirmed = true;
      identity.updatedAt = System.currentTimeMillis();
      write(origin, storageKey, identity);
    }
  }

  RotationMaterial beginRotation(String rawOrigin)
      throws IdentityLostException, IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      assertActive(identity);
      if (identity.pendingRotationId == null || identity.pendingDeviceSecret == null) {
        identity.pendingRotationId = newSecret();
        identity.pendingDeviceSecret = newSecret();
        identity.updatedAt = System.currentTimeMillis();
        write(origin, storageKey, identity);
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
      throws IdentityLostException, IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      assertActive(identity);
      assertRotation(identity, rotationId);
      identity.deviceSecret = identity.pendingDeviceSecret;
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
      identity.updatedAt = System.currentTimeMillis();
      write(origin, storageKey, identity);
    }
  }

  void abortRotation(String rawOrigin, String rotationId)
      throws IdentityLostException, IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      assertActive(identity);
      assertRotation(identity, rotationId);
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
      identity.updatedAt = System.currentTimeMillis();
      write(origin, storageKey, identity);
    }
  }

  void markIdentityLost(String rawOrigin) throws IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      identity.lost = true;
      identity.updatedAt = System.currentTimeMillis();
      write(origin, storageKey, identity);
    }
  }

  void restoreIdentity(String rawOrigin) throws IdentityMissingException, VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      String storageKey = IdentityOrigin.storageKey(origin);
      StoredIdentity identity = readExisting(origin, storageKey);
      if (!identity.lost) {
        return;
      }
      identity.lost = false;
      identity.updatedAt = System.currentTimeMillis();
      write(origin, storageKey, identity);
    }
  }

  void clear(String rawOrigin) throws VaultException {
    String origin = IdentityOrigin.normalize(rawOrigin);
    synchronized (PROCESS_LOCK) {
      if (!preferences.edit().remove(IdentityOrigin.storageKey(origin)).commit()) {
        throw new VaultException("secure_identity_clear_failed");
      }
    }
  }

  boolean isKeyNonExportable() throws VaultException {
    synchronized (PROCESS_LOCK) {
      return getOrCreateKey().getEncoded() == null;
    }
  }

  boolean isKeyHardwareBacked() throws VaultException {
    synchronized (PROCESS_LOCK) {
      try {
        SecretKey key = getOrCreateKey();
        SecretKeyFactory factory = SecretKeyFactory.getInstance(key.getAlgorithm(), KEYSTORE_PROVIDER);
        KeyInfo info = (KeyInfo) factory.getKeySpec(key, KeyInfo.class);
        return info.isInsideSecureHardware();
      } catch (GeneralSecurityException error) {
        throw new VaultException("secure_identity_key_info_failed", error);
      }
    }
  }

  private StoredIdentity readExisting(String origin, String storageKey)
      throws IdentityMissingException, VaultException {
    String ciphertext = preferences.getString(storageKey, null);
    if (ciphertext == null) {
      throw new IdentityMissingException();
    }
    return decryptRecord(origin, ciphertext);
  }

  private void write(String origin, String storageKey, StoredIdentity identity)
      throws VaultException {
    String ciphertext = encryptRecord(origin, identity);
    if (!preferences.edit().putString(storageKey, ciphertext).commit()) {
      throw new VaultException("secure_identity_write_failed");
    }
  }

  private String encryptRecord(String origin, StoredIdentity identity) throws VaultException {
    try {
      Cipher cipher = Cipher.getInstance(CIPHER);
      // Android Keystore enforces randomized encryption and therefore must generate the IV.
      cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
      byte[] iv = cipher.getIV();
      if (iv == null || iv.length != IV_BYTES) {
        throw new GeneralSecurityException("Android Keystore returned an invalid GCM IV");
      }
      cipher.updateAAD(aad(origin));
      byte[] encrypted = cipher.doFinal(toJson(identity).getBytes(StandardCharsets.UTF_8));
      ByteBuffer blob = ByteBuffer.allocate(2 + iv.length + encrypted.length);
      blob.put((byte) BLOB_VERSION);
      blob.put((byte) iv.length);
      blob.put(iv);
      blob.put(encrypted);
      return Base64.encodeToString(blob.array(), Base64.NO_WRAP | Base64.URL_SAFE);
    } catch (GeneralSecurityException | JSONException error) {
      throw new VaultException("secure_identity_encrypt_failed", error);
    }
  }

  private StoredIdentity decryptRecord(String origin, String encoded) throws VaultException {
    try {
      byte[] bytes = Base64.decode(encoded, Base64.NO_WRAP | Base64.URL_SAFE);
      if (bytes.length < 2 + IV_BYTES + 16) {
        throw new GeneralSecurityException("identity ciphertext is truncated");
      }
      ByteBuffer blob = ByteBuffer.wrap(bytes);
      int version = blob.get() & 0xff;
      int ivLength = blob.get() & 0xff;
      if (version != BLOB_VERSION || ivLength != IV_BYTES || blob.remaining() <= ivLength) {
        throw new GeneralSecurityException("identity ciphertext format is invalid");
      }
      byte[] iv = new byte[ivLength];
      blob.get(iv);
      byte[] encrypted = new byte[blob.remaining()];
      blob.get(encrypted);

      Cipher cipher = Cipher.getInstance(CIPHER);
      cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), new GCMParameterSpec(GCM_TAG_BITS, iv));
      cipher.updateAAD(aad(origin));
      String json = new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
      return fromJson(json);
    } catch (GeneralSecurityException | JSONException | IllegalArgumentException error) {
      throw new VaultException("secure_identity_unreadable", error);
    }
  }

  private SecretKey getOrCreateKey() throws VaultException {
    try {
      KeyStore store = KeyStore.getInstance(KEYSTORE_PROVIDER);
      store.load(null);
      java.security.Key existing = store.getKey(KEY_ALIAS, null);
      if (existing instanceof SecretKey) {
        return (SecretKey) existing;
      }
      if (existing != null) {
        throw new GeneralSecurityException("identity key alias has an unexpected type");
      }

      KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE_PROVIDER);
      generator.init(
          new KeyGenParameterSpec.Builder(
              KEY_ALIAS,
              KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
          )
              .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
              .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
              .setKeySize(KEY_BITS)
              .setRandomizedEncryptionRequired(true)
              .setUserAuthenticationRequired(false)
              .build()
      );
      return generator.generateKey();
    } catch (Exception error) {
      throw new VaultException("secure_identity_keystore_unavailable", error);
    }
  }

  private static byte[] aad(String origin) {
    return ("echodesk.identity.v1\u0000" + origin).getBytes(StandardCharsets.UTF_8);
  }

  private String newSecret() {
    byte[] value = new byte[SECRET_BYTES];
    random.nextBytes(value);
    return Base64.encodeToString(value, Base64.NO_WRAP | Base64.NO_PADDING | Base64.URL_SAFE);
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

  private static void assertActive(StoredIdentity identity) throws IdentityLostException {
    if (identity.lost) {
      throw new IdentityLostException();
    }
  }

  private static void assertRotation(StoredIdentity identity, String rotationId)
      throws VaultException {
    if (rotationId == null
        || identity.pendingRotationId == null
        || identity.pendingDeviceSecret == null
        || !identity.pendingRotationId.equals(rotationId)) {
      throw new VaultException("secure_identity_rotation_mismatch");
    }
  }

  private static String toJson(StoredIdentity identity) throws JSONException {
    JSONObject value = new JSONObject();
    value.put("version", RECORD_VERSION);
    value.put("enrollmentId", identity.enrollmentId);
    value.put("deviceSecret", identity.deviceSecret);
    value.put("enrollmentConfirmed", identity.enrollmentConfirmed);
    value.put("lost", identity.lost);
    value.put("createdAt", identity.createdAt);
    value.put("updatedAt", identity.updatedAt);
    if (identity.pendingRotationId != null && identity.pendingDeviceSecret != null) {
      value.put("pendingRotationId", identity.pendingRotationId);
      value.put("pendingDeviceSecret", identity.pendingDeviceSecret);
    }
    return value.toString();
  }

  private static StoredIdentity fromJson(String json) throws JSONException {
    JSONObject value = new JSONObject(json);
    if (value.getInt("version") != RECORD_VERSION) {
      throw new JSONException("unsupported identity record version");
    }
    StoredIdentity identity = new StoredIdentity();
    identity.enrollmentId = value.getString("enrollmentId");
    identity.deviceSecret = value.getString("deviceSecret");
    // Records written before this flag existed must safely replay idempotent enrollment.
    identity.enrollmentConfirmed = value.optBoolean("enrollmentConfirmed", false);
    identity.lost = value.optBoolean("lost", false);
    identity.createdAt = value.getLong("createdAt");
    identity.updatedAt = value.getLong("updatedAt");
    identity.pendingRotationId = value.optString("pendingRotationId", null);
    identity.pendingDeviceSecret = value.optString("pendingDeviceSecret", null);
    if (identity.enrollmentId.length() < 32 || identity.deviceSecret.length() < 32) {
      throw new JSONException("identity record secret is invalid");
    }
    if ((identity.pendingRotationId == null) != (identity.pendingDeviceSecret == null)) {
      throw new JSONException("identity rotation record is incomplete");
    }
    return identity;
  }
}

package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.SharedPreferences;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class AndroidIdentityVaultTest {
  private static final String ORIGIN = "https://identity-test.example";
  private static final String OTHER_ORIGIN = "https://identity-other.example:8443";

  private Context context;
  private AndroidIdentityVault vault;

  @Before
  public void setUp() throws Exception {
    context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    vault = new AndroidIdentityVault(context);
    vault.clear(ORIGIN);
    vault.clear(OTHER_ORIGIN);
  }

  @After
  public void tearDown() throws Exception {
    vault.clear(ORIGIN);
    vault.clear(OTHER_ORIGIN);
  }

  @Test
  public void identitySurvivesVaultReloadAndPreferencesContainCiphertextOnly() throws Exception {
    AndroidIdentityVault.IdentityMaterial first = vault.loadOrCreate(ORIGIN);
    AndroidIdentityVault.IdentityMaterial reloaded =
        new AndroidIdentityVault(context).loadOrCreate(ORIGIN);

    assertTrue(first.created);
    assertFalse(reloaded.created);
    assertFalse(first.enrollmentConfirmed);
    assertFalse(reloaded.enrollmentConfirmed);
    assertEquals(first.enrollmentId, reloaded.enrollmentId);
    assertEquals(first.deviceSecret, reloaded.deviceSecret);
    assertTrue(vault.isKeyNonExportable());

    vault.confirmEnrollment(ORIGIN);
    AndroidIdentityVault.IdentityMaterial confirmed =
        new AndroidIdentityVault(context).loadOrCreate(ORIGIN);
    assertTrue(confirmed.enrollmentConfirmed);
    assertEquals(first.enrollmentId, confirmed.enrollmentId);
    assertEquals(first.deviceSecret, confirmed.deviceSecret);

    SharedPreferences preferences = context.getSharedPreferences(
        AndroidIdentityVault.PREFERENCES_NAME,
        Context.MODE_PRIVATE
    );
    String ciphertext = preferences.getString(
        IdentityOrigin.storageKey(IdentityOrigin.normalize(ORIGIN)),
        ""
    );
    assertFalse(ciphertext.isEmpty());
    assertFalse(ciphertext.contains(first.enrollmentId));
    assertFalse(ciphertext.contains(first.deviceSecret));
  }

  @Test
  public void recordsAreSeparatedAndAuthenticatedByOrigin() throws Exception {
    AndroidIdentityVault.IdentityMaterial first = vault.loadOrCreate(ORIGIN);
    AndroidIdentityVault.IdentityMaterial other = vault.loadOrCreate(OTHER_ORIGIN);
    assertNotEquals(first.enrollmentId, other.enrollmentId);
    assertNotEquals(first.deviceSecret, other.deviceSecret);

    SharedPreferences preferences = context.getSharedPreferences(
        AndroidIdentityVault.PREFERENCES_NAME,
        Context.MODE_PRIVATE
    );
    String firstKey = IdentityOrigin.storageKey(IdentityOrigin.normalize(ORIGIN));
    String otherKey = IdentityOrigin.storageKey(IdentityOrigin.normalize(OTHER_ORIGIN));
    String firstCiphertext = preferences.getString(firstKey, "");
    assertTrue(preferences.edit().putString(otherKey, firstCiphertext).commit());

    assertThrows(
        AndroidIdentityVault.VaultException.class,
        () -> new AndroidIdentityVault(context).loadOrCreate(OTHER_ORIGIN)
    );
  }

  @Test
  public void lostIdentityCannotSilentlyBecomeANewOwner() throws Exception {
    AndroidIdentityVault.IdentityMaterial first = vault.loadOrCreate(ORIGIN);
    vault.markIdentityLost(ORIGIN);

    assertThrows(
        AndroidIdentityVault.IdentityLostException.class,
        () -> new AndroidIdentityVault(context).loadOrCreate(ORIGIN)
    );

    AndroidIdentityVault.IdentityMaterial reconnect =
        new AndroidIdentityVault(context).loadForReconnect(ORIGIN);
    assertEquals(first.enrollmentId, reconnect.enrollmentId);
    assertEquals(first.deviceSecret, reconnect.deviceSecret);
    assertThrows(
        AndroidIdentityVault.IdentityLostException.class,
        () -> new AndroidIdentityVault(context).loadOrCreate(ORIGIN)
    );

    new AndroidIdentityVault(context).restoreIdentity(ORIGIN);
    AndroidIdentityVault.IdentityMaterial restored = vault.loadOrCreate(ORIGIN);
    assertEquals(first.enrollmentId, restored.enrollmentId);
    assertEquals(first.deviceSecret, restored.deviceSecret);

    vault.clear(ORIGIN);
    AndroidIdentityVault.IdentityMaterial explicitlyReset = vault.loadOrCreate(ORIGIN);
    assertNotEquals(first.enrollmentId, explicitlyReset.enrollmentId);
    assertNotEquals(first.deviceSecret, explicitlyReset.deviceSecret);
  }

  @Test
  public void rotationIsIdempotentlyStagedAndAtomicallyCommitted() throws Exception {
    AndroidIdentityVault.IdentityMaterial first = vault.loadOrCreate(ORIGIN);
    AndroidIdentityVault.RotationMaterial pending = vault.beginRotation(ORIGIN);
    AndroidIdentityVault.RotationMaterial pendingAgain =
        new AndroidIdentityVault(context).beginRotation(ORIGIN);
    AndroidIdentityVault.IdentityMaterial withPending =
        new AndroidIdentityVault(context).loadOrCreate(ORIGIN);

    assertEquals(pending.rotationId, pendingAgain.rotationId);
    assertEquals(pending.newDeviceSecret, pendingAgain.newDeviceSecret);
    assertEquals(pending.rotationId, withPending.pendingRotationId);
    assertEquals(pending.newDeviceSecret, withPending.pendingDeviceSecret);
    assertEquals(first.deviceSecret, pending.currentDeviceSecret);
    assertNotEquals(first.deviceSecret, pending.newDeviceSecret);

    new AndroidIdentityVault(context).commitRotation(ORIGIN, pending.rotationId);
    AndroidIdentityVault.IdentityMaterial rotated = vault.loadOrCreate(ORIGIN);
    assertEquals(pending.newDeviceSecret, rotated.deviceSecret);
    assertNotEquals(first.deviceSecret, rotated.deviceSecret);
    assertEquals(null, rotated.pendingRotationId);
    assertEquals(null, rotated.pendingDeviceSecret);
  }

  @Test
  public void rotationAbortSurvivesReloadAndMismatchFailsClosed() throws Exception {
    AndroidIdentityVault.IdentityMaterial first = vault.loadOrCreate(ORIGIN);
    AndroidIdentityVault.RotationMaterial pending = vault.beginRotation(ORIGIN);

    AndroidIdentityVault.VaultException mismatch = assertThrows(
        AndroidIdentityVault.VaultException.class,
        () -> new AndroidIdentityVault(context).commitRotation(ORIGIN, "wrong-rotation-id")
    );
    assertEquals("secure_identity_rotation_mismatch", mismatch.getMessage());

    new AndroidIdentityVault(context).abortRotation(ORIGIN, pending.rotationId);
    AndroidIdentityVault.IdentityMaterial reloaded =
        new AndroidIdentityVault(context).loadOrCreate(ORIGIN);
    assertEquals(first.deviceSecret, reloaded.deviceSecret);
    assertEquals(null, reloaded.pendingRotationId);
    assertEquals(null, reloaded.pendingDeviceSecret);
  }
}

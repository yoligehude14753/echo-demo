package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import org.junit.After;
import org.junit.Test;

public class AndroidMemoryIdentityStoreTest {
  private static final String ORIGIN = "https://identity-test.example";
  private static final String OTHER_ORIGIN = "https://identity-other.example:8443";

  @After
  public void clearProcessIdentity() {
    AndroidMemoryIdentityStore.clearProcessForTest();
  }

  @Test
  public void identityStaysInProcessAndNewStoreSharesTheProcessMap() throws Exception {
    AndroidMemoryIdentityStore firstStore = new AndroidMemoryIdentityStore();
    AndroidMemoryIdentityStore.IdentityMaterial first = firstStore.loadOrCreate(ORIGIN);
    AndroidMemoryIdentityStore.IdentityMaterial sameProcess =
        new AndroidMemoryIdentityStore().loadOrCreate(ORIGIN);

    assertTrue(first.created);
    assertFalse(sameProcess.created);
    assertEquals(first.enrollmentId, sameProcess.enrollmentId);
    assertEquals(first.deviceSecret, sameProcess.deviceSecret);
  }

  @Test
  public void processResetDropsCredentialAndNextBootstrapCreatesNewIdentity() throws Exception {
    AndroidMemoryIdentityStore.IdentityMaterial first =
        new AndroidMemoryIdentityStore().loadOrCreate(ORIGIN);
    AndroidMemoryIdentityStore.clearProcessForTest();

    AndroidMemoryIdentityStore.IdentityMissingException missing = assertThrows(
        AndroidMemoryIdentityStore.IdentityMissingException.class,
        () -> new AndroidMemoryIdentityStore().loadForReconnect(ORIGIN)
    );
    assertEquals("identity_missing", missing.getMessage());

    AndroidMemoryIdentityStore.IdentityMaterial bootstrapped =
        new AndroidMemoryIdentityStore().loadOrCreate(ORIGIN);
    assertTrue(bootstrapped.created);
    assertNotEquals(first.enrollmentId, bootstrapped.enrollmentId);
    assertNotEquals(first.deviceSecret, bootstrapped.deviceSecret);
  }

  @Test
  public void identityStateAndRotationRemainAtomicInMemory() throws Exception {
    AndroidMemoryIdentityStore store = new AndroidMemoryIdentityStore();
    AndroidMemoryIdentityStore.IdentityMaterial first = store.loadOrCreate(ORIGIN);
    AndroidMemoryIdentityStore.RotationMaterial pending = store.beginRotation(ORIGIN);
    AndroidMemoryIdentityStore.RotationMaterial pendingAgain = store.beginRotation(ORIGIN);

    assertEquals(pending.rotationId, pendingAgain.rotationId);
    assertEquals(pending.newDeviceSecret, pendingAgain.newDeviceSecret);
    assertEquals(first.deviceSecret, pending.currentDeviceSecret);

    AndroidMemoryIdentityStore.StoreException mismatch = assertThrows(
        AndroidMemoryIdentityStore.StoreException.class,
        () -> store.commitRotation(ORIGIN, "wrong-rotation-id")
    );
    assertEquals("memory_identity_rotation_mismatch", mismatch.getMessage());

    store.commitRotation(ORIGIN, pending.rotationId);
    AndroidMemoryIdentityStore.IdentityMaterial rotated = store.loadOrCreate(ORIGIN);
    assertEquals(pending.newDeviceSecret, rotated.deviceSecret);
    assertEquals(null, rotated.pendingRotationId);
    assertEquals(null, rotated.pendingDeviceSecret);
  }

  @Test
  public void lostIdentityDoesNotSilentlyEnrollUntilExplicitRestore() throws Exception {
    AndroidMemoryIdentityStore store = new AndroidMemoryIdentityStore();
    AndroidMemoryIdentityStore.IdentityMaterial first = store.loadOrCreate(ORIGIN);
    store.markIdentityLost(ORIGIN);

    assertThrows(
        AndroidMemoryIdentityStore.IdentityLostException.class,
        () -> store.loadOrCreate(ORIGIN)
    );
    AndroidMemoryIdentityStore.IdentityMaterial reconnect = store.loadForReconnect(ORIGIN);
    assertEquals(first.deviceSecret, reconnect.deviceSecret);

    store.restoreIdentity(ORIGIN);
    AndroidMemoryIdentityStore.IdentityMaterial restored = store.loadOrCreate(ORIGIN);
    assertEquals(first.deviceSecret, restored.deviceSecret);
    assertNotEquals(first.deviceSecret, store.loadOrCreate(OTHER_ORIGIN).deviceSecret);
  }
}

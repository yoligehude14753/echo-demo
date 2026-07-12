package com.echodesk.app;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "EchoIdentity")
public final class EchoIdentityPlugin extends Plugin {
  private AndroidIdentityVault vault;

  @Override
  public void load() {
    vault = new AndroidIdentityVault(getContext());
  }

  @PluginMethod
  public void capabilities(PluginCall call) {
    try {
      boolean nonExportable = vault.isKeyNonExportable();
      if (!nonExportable) {
        call.reject("secure identity key is exportable", "SECURE_STORE_ERROR");
        return;
      }
      boolean hardwareBacked;
      try {
        hardwareBacked = vault.isKeyHardwareBacked();
      } catch (AndroidIdentityVault.VaultException unavailableInfo) {
        hardwareBacked = false;
      }
      JSObject result = new JSObject();
      result.put("runtime", "android-keystore");
      result.put("persistence", "secure-device");
      result.put("durable", true);
      result.put("originBound", true);
      result.put("atomicRotation", true);
      result.put("keyNonExportable", true);
      result.put("hardwareBacked", hardwareBacked);
      call.resolve(result);
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void loadOrCreate(PluginCall call) {
    try {
      AndroidIdentityVault.IdentityMaterial identity = vault.loadOrCreate(requiredOrigin(call));
      JSObject result = new JSObject();
      result.put("origin", identity.origin);
      result.put("enrollmentId", identity.enrollmentId);
      result.put("deviceSecret", identity.deviceSecret);
      result.put("created", identity.created);
      result.put("enrollmentConfirmed", identity.enrollmentConfirmed);
      result.put("pendingRotationId", identity.pendingRotationId);
      result.put("pendingDeviceSecret", identity.pendingDeviceSecret);
      call.resolve(result);
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void loadForReconnect(PluginCall call) {
    try {
      AndroidIdentityVault.IdentityMaterial identity =
          vault.loadForReconnect(requiredOrigin(call));
      JSObject result = new JSObject();
      result.put("origin", identity.origin);
      result.put("enrollmentId", identity.enrollmentId);
      result.put("deviceSecret", identity.deviceSecret);
      result.put("created", false);
      result.put("enrollmentConfirmed", identity.enrollmentConfirmed);
      result.put("pendingRotationId", identity.pendingRotationId);
      result.put("pendingDeviceSecret", identity.pendingDeviceSecret);
      call.resolve(result);
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void confirmEnrollment(PluginCall call) {
    try {
      vault.confirmEnrollment(requiredOrigin(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void beginRotation(PluginCall call) {
    try {
      AndroidIdentityVault.RotationMaterial rotation = vault.beginRotation(requiredOrigin(call));
      JSObject result = new JSObject();
      result.put("origin", rotation.origin);
      result.put("rotationId", rotation.rotationId);
      result.put("currentDeviceSecret", rotation.currentDeviceSecret);
      result.put("newDeviceSecret", rotation.newDeviceSecret);
      call.resolve(result);
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void commitRotation(PluginCall call) {
    try {
      vault.commitRotation(requiredOrigin(call), requiredRotationId(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void abortRotation(PluginCall call) {
    try {
      vault.abortRotation(requiredOrigin(call), requiredRotationId(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void markIdentityLost(PluginCall call) {
    try {
      vault.markIdentityLost(requiredOrigin(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void restoreIdentity(PluginCall call) {
    try {
      vault.restoreIdentity(requiredOrigin(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  @PluginMethod
  public void clear(PluginCall call) {
    try {
      vault.clear(requiredOrigin(call));
      call.resolve(ok());
    } catch (Exception error) {
      reject(call, error);
    }
  }

  private static JSObject ok() {
    JSObject result = new JSObject();
    result.put("ok", true);
    return result;
  }

  private static String requiredOrigin(PluginCall call) {
    String origin = call.getString("origin");
    if (origin == null || origin.trim().isEmpty()) {
      throw new IllegalArgumentException("backend origin is required");
    }
    return origin;
  }

  private static String requiredRotationId(PluginCall call) {
    String rotationId = call.getString("rotationId");
    if (rotationId == null || rotationId.trim().isEmpty()) {
      throw new IllegalArgumentException("rotation id is required");
    }
    return rotationId;
  }

  private static void reject(PluginCall call, Exception error) {
    if (error instanceof AndroidIdentityVault.IdentityLostException) {
      call.reject("identity_lost", "IDENTITY_LOST");
      return;
    }
    if (error instanceof AndroidIdentityVault.IdentityMissingException) {
      call.reject("identity_missing", "IDENTITY_MISSING");
      return;
    }
    if (error instanceof IllegalArgumentException) {
      call.reject("invalid backend origin", "INVALID_ORIGIN");
      return;
    }
    if (error instanceof AndroidIdentityVault.VaultException
        && "secure_identity_rotation_mismatch".equals(error.getMessage())) {
      call.reject("secure_identity_rotation_mismatch", "ROTATION_MISMATCH");
      return;
    }
    call.reject("secure identity store unavailable", "SECURE_STORE_ERROR", error);
  }
}

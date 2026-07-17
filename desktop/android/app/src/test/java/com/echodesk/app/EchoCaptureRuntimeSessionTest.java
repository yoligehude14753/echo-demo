package com.echodesk.app;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class EchoCaptureRuntimeSessionTest {
  @Test
  public void missingSessionFieldsStayFailClosedWithoutThrowing() {
    assertFalse(EchoCaptureRuntime.hasUploadSession(null, null, null));
    assertFalse(EchoCaptureRuntime.hasUploadSession("", "token", "device"));
    assertFalse(EchoCaptureRuntime.hasUploadSession("https://echodesk.yoliyoli.uk", null, "device"));
    assertFalse(EchoCaptureRuntime.hasUploadSession("https://echodesk.yoliyoli.uk", "token", null));
  }

  @Test
  public void blankSessionFieldsStayFailClosedWithoutThrowing() {
    assertFalse(EchoCaptureRuntime.hasUploadSession("   ", "token", "device"));
    assertFalse(EchoCaptureRuntime.hasUploadSession("https://echodesk.yoliyoli.uk", "   ", "device"));
    assertFalse(EchoCaptureRuntime.hasUploadSession("https://echodesk.yoliyoli.uk", "token", "   "));
  }

  @Test
  public void completeSessionAllowsNativeUpload() {
    assertTrue(
        EchoCaptureRuntime.hasUploadSession(
            " https://echodesk.yoliyoli.uk/ ",
            " bearer-token ",
            " device-123 "
        )
    );
  }

  @Test
  public void nativeQueueRequiresLiveUnblockedBearerSession() {
    assertFalse(
        EchoCaptureRuntime.canQueueNativeCapture(
            "https://echodesk.yoliyoli.uk", "", "device-123", false
        )
    );
    assertFalse(
        EchoCaptureRuntime.canQueueNativeCapture(
            "https://echodesk.yoliyoli.uk", "token", "device-123", true
        )
    );
    assertTrue(
        EchoCaptureRuntime.canQueueNativeCapture(
            "https://echodesk.yoliyoli.uk", "token", "device-123", false
        )
    );
  }
}

package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

import org.junit.Test;

public class EchoCaptureRuntimeResponseTest {
  @Test
  public void parsesOnlyAllowlistedSuccessFieldsAndHashesText() {
    EchoCaptureRuntime.UploadResponse response = EchoCaptureRuntime.parseUploadResponse(
        200,
        "{\"ambient_stored\":true,\"stt_status\":\"ok\","
            + "\"ambient_text\":\"这是受控文本\",\"token\":\"must-not-be-logged\"}"
    );

    assertTrue(response.ambientStored);
    assertEquals("ok", response.sttStatus);
    assertEquals(
        "aedbb5272013c6ac2730c03dba6b3556fb99af7c6b19ef1e2ed42720bc411da9",
        response.textSha256
    );
  }

  @Test
  public void nonSuccessResponsesDoNotExposeBodyFields() {
    EchoCaptureRuntime.UploadResponse response = EchoCaptureRuntime.parseUploadResponse(
        426,
        "{\"ambient_stored\":true,\"stt_status\":\"ok\",\"ambient_text\":\"secret\"}"
    );

    assertFalse(response.ambientStored);
    assertEquals("unknown", response.sttStatus);
    assertEquals("", response.textSha256);
  }

  @Test
  public void responseBodyReadIsBounded() throws Exception {
    byte[] body = new byte[EchoCaptureRuntime.MAX_UPLOAD_RESPONSE_BYTES + 17];
    Arrays.fill(body, (byte) 'x');

    String read = EchoCaptureRuntime.readBoundedResponse(new ByteArrayInputStream(body));

    assertEquals(
        EchoCaptureRuntime.MAX_UPLOAD_RESPONSE_BYTES,
        read.getBytes(StandardCharsets.UTF_8).length
    );
  }
}

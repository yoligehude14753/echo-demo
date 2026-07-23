package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;

public class IdentityOriginTest {
  @Test
  public void normalizeCanonicalizesSchemeHostAndDefaultPorts() {
    assertEquals(
        "https://example.com",
        IdentityOrigin.normalize(" HTTPS://Example.COM:443/api/path?ignored=true ")
    );
  }

  @Test
  public void normalizeRejectsOpaqueOrCredentialBearingOrigins() {
    assertThrows(
        IllegalArgumentException.class,
        () -> IdentityOrigin.normalize("file:///tmp/backend")
    );
    assertThrows(
        IllegalArgumentException.class,
        () -> IdentityOrigin.normalize("https://user:password@example.com")
    );
    assertThrows(
        IllegalArgumentException.class,
        () -> IdentityOrigin.normalize("example.com")
    );
    assertThrows(
        IllegalArgumentException.class,
        () -> IdentityOrigin.normalize("http://127.0.0.1:8769")
    );
    assertThrows(
        IllegalArgumentException.class,
        () -> IdentityOrigin.normalize("http://192.168.199.179:8769")
    );
  }
}

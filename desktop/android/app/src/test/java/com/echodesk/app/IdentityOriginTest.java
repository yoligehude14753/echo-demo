package com.echodesk.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

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

  @Test
  public void storageKeyIsStableAndOriginSpecificWithoutEmbeddingTheOrigin() {
    String first = IdentityOrigin.storageKey("https://example.com");
    String same = IdentityOrigin.storageKey("https://example.com");
    String other = IdentityOrigin.storageKey("https://other.example");

    assertEquals(first, same);
    assertNotEquals(first, other);
    assertTrue(first.startsWith("identity."));
    assertEquals("identity.".length() + 64, first.length());
    assertTrue(!first.contains("example.com"));
  }
}

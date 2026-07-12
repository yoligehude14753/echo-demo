import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

function bootstrapBody(sessionRequired: boolean): string {
  return JSON.stringify({
    schema_version: 1,
    api_version: "0.3",
    backend_version: "0.3.1-test",
    session_required: sessionRequired,
    capabilities: { principal_sessions: true },
  });
}

test("lost enrollment response retries the same id and secret before confirming", async ({
  page,
}) => {
  let sessionRequired = false;
  const enrollmentBodies: string[] = [];
  let renewCalls = 0;
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    enrollmentBodies.push(route.request().postData() ?? "");
    if (enrollmentBodies.length === 1) return route.abort("connectionreset");
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "enrollment-retry-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    renewCalls += 1;
    return route.fulfill({ status: 500, body: "renew must not run" });
  });

  await page.goto("/");
  sessionRequired = true;
  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    let firstFailed = false;
    try {
      await session.ensureServerSession();
    } catch {
      firstFailed = true;
    }
    const afterFailure = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    const token = await session.ensureServerSession();
    const afterSuccess = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    return { firstFailed, afterFailure, afterSuccess, token };
  });

  expect(result.firstFailed).toBe(true);
  expect(result.afterFailure.enrollment_confirmed).toBe(false);
  expect(result.afterSuccess.enrollment_confirmed).toBe(true);
  expect(result.afterSuccess.enrollment_id).toBe(
    result.afterFailure.enrollment_id,
  );
  expect(result.afterSuccess.device_secret).toBe(
    result.afterFailure.device_secret,
  );
  expect(result.token).toBe("enrollment-retry-token");
  expect(enrollmentBodies).toHaveLength(2);
  expect(enrollmentBodies[1]).toBe(enrollmentBodies[0]);
  expect(renewCalls).toBe(0);
});

test("429, 5xx and network renewal errors retain the confirmed owner", async ({
  page,
}) => {
  let sessionRequired = false;
  let enrollCalls = 0;
  let renewMode: "429" | "503" | "network" | "ok" = "429";
  const renewBodies: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    enrollCalls += 1;
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "confirmed-owner-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    renewBodies.push(route.request().postData() ?? "");
    if (renewMode === "network") return route.abort("connectionreset");
    if (renewMode !== "ok") {
      return route.fulfill({ status: Number(renewMode), body: renewMode });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: "renewed-owner-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });

  await page.goto("/");
  sessionRequired = true;
  await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    await session.ensureServerSession();
  });

  const phases: string[] = [];
  for (const mode of ["429", "503", "network"] as const) {
    renewMode = mode;
    phases.push(
      await page.evaluate(async () => {
        const session = await import("/src/session.ts");
        try {
          await session.ensureServerSession(true);
        } catch {
          // Expected transient failure.
        }
        return session.currentSessionIdentityStatus().phase;
      }),
    );
  }
  renewMode = "ok";
  const final = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    const token = await session.ensureServerSession(true);
    const identity = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    return {
      token,
      phase: session.currentSessionIdentityStatus().phase,
      identity,
    };
  });

  expect(phases).toEqual(["error", "error", "error"]);
  expect(final.phase).toBe("ready");
  expect(final.token).toBe("renewed-owner-token");
  expect(final.identity.enrollment_confirmed).toBe(true);
  expect(enrollCalls).toBe(1);
  expect(renewBodies).toHaveLength(4);
  expect(new Set(renewBodies).size).toBe(1);
});

test("pending rotation survives a transient error and commits when the new secret renews", async ({
  page,
}) => {
  let sessionRequired = false;
  let renewMode: "transient" | "new-active" = "transient";
  let activeCredential = "";
  const attemptedCredentials: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_secret?: string;
    };
    activeCredential = body.device_secret ?? "";
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "rotation-base-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_credential?: string;
    };
    attemptedCredentials.push(body.device_credential ?? "");
    if (renewMode === "transient") {
      return route.fulfill({ status: 503, body: "temporarily unavailable" });
    }
    const ok = body.device_credential === activeCredential;
    return route.fulfill({
      status: ok ? 200 : 401,
      contentType: "application/json",
      body: ok
        ? JSON.stringify({
            token: "rotation-recovered-token",
            expires_at: "2099-01-01T00:00:00Z",
          })
        : "identity_lost",
    });
  });

  await page.goto("/");
  sessionRequired = true;
  const staged = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    await session.ensureServerSession();
    return identityCredentialStore.beginRotation(window.location.origin);
  });

  const transient = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    try {
      await session.ensureServerSession(true);
    } catch {
      // Expected transient failure.
    }
    return identityCredentialStore.loadOrCreate(window.location.origin);
  });
  expect(transient.pending_rotation).toEqual(staged);

  renewMode = "new-active";
  activeCredential = staged.new_device_credential;
  const recovered = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    const token = await session.ensureServerSession(true);
    const identity = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    return { token, identity };
  });

  expect(recovered.token).toBe("rotation-recovered-token");
  expect(recovered.identity.device_secret).toBe(staged.new_device_credential);
  expect(recovered.identity.pending_rotation).toBeNull();
  expect(attemptedCredentials).toEqual([
    staged.new_device_credential,
    staged.new_device_credential,
  ]);
});

test("pending rotation 401 falls back to current secret and aborts pending", async ({
  page,
}) => {
  let sessionRequired = false;
  let activeCredential = "";
  const attemptedCredentials: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_secret?: string;
    };
    activeCredential = body.device_secret ?? "";
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "rotation-current-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_credential?: string;
    };
    const credential = body.device_credential ?? "";
    attemptedCredentials.push(credential);
    const ok = credential === activeCredential;
    return route.fulfill({
      status: ok ? 200 : 401,
      contentType: "application/json",
      body: ok
        ? JSON.stringify({
            token: "rotation-current-recovered",
            expires_at: "2099-01-01T00:00:00Z",
          })
        : "identity_lost",
    });
  });

  await page.goto("/");
  sessionRequired = true;
  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    await session.ensureServerSession();
    const staged = await identityCredentialStore.beginRotation(
      window.location.origin,
    );
    const token = await session.ensureServerSession(true);
    const identity = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    return { staged, token, identity };
  });

  expect(result.token).toBe("rotation-current-recovered");
  expect(result.identity.device_secret).toBe(
    result.staged.current_device_credential,
  );
  expect(result.identity.pending_rotation).toBeNull();
  expect(attemptedCredentials).toEqual([
    result.staged.new_device_credential,
    result.staged.current_device_credential,
  ]);
});

test("active renderer rotation commits only after the server returns 200", async ({
  page,
}) => {
  let sessionRequired = false;
  let rotateAuthorization = "";
  let rotateBody: {
    current_device_credential?: string;
    new_device_credential?: string;
  } = {};
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/credential/rotate"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) =>
    route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "rotation-active-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    }),
  );
  await page.route(/\/(api\/)?session\/credential\/rotate$/, (route) => {
    rotateAuthorization = route.request().headers().authorization ?? "";
    rotateBody = JSON.parse(route.request().postData() ?? "{}");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        credential_id: "credential-rotated",
        credential_expires_at: "2099-06-01T00:00:00Z",
      }),
    });
  });

  await page.goto("/");
  sessionRequired = true;
  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    await session.ensureServerSession();
    const before = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    const rotation = await session.rotateServerCredential();
    const after = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    return { before, rotation, after };
  });

  expect(rotateAuthorization).toBe("Bearer rotation-active-token");
  expect(rotateBody.current_device_credential).toBe(result.before.device_secret);
  expect(rotateBody.new_device_credential).not.toBe(result.before.device_secret);
  expect(result.after.device_secret).toBe(rotateBody.new_device_credential);
  expect(result.after.pending_rotation).toBeNull();
  expect(result.rotation).toEqual({
    credential_id: "credential-rotated",
    credential_expires_at: "2099-06-01T00:00:00Z",
  });
});

test("rotation 409 stays pending until explicit reconnect proves the current credential", async ({
  page,
}) => {
  let sessionRequired = false;
  let activeCredential = "";
  const renewCredentials: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: [
      "/bootstrap",
      "/session/enroll",
      "/session/renew",
      "/session/credential/rotate",
    ],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_secret?: string;
    };
    activeCredential = body.device_secret ?? "";
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "rotation-conflict-token",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/credential\/rotate$/, (route) =>
    route.fulfill({ status: 409, body: "credential_conflict" }),
  );
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_credential?: string;
    };
    const credential = body.device_credential ?? "";
    renewCredentials.push(credential);
    const accepted = credential === activeCredential;
    return route.fulfill({
      status: accepted ? 200 : 401,
      contentType: "application/json",
      body: accepted
        ? JSON.stringify({
            token: "rotation-current-restored",
            expires_at: "2099-01-01T00:00:00Z",
          })
        : "identity_lost",
    });
  });

  await page.goto("/");
  sessionRequired = true;
  const rejected = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    await session.ensureServerSession();
    const before = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    let kind = "";
    try {
      await session.rotateServerCredential();
    } catch (error) {
      kind = (error as { kind?: string }).kind ?? "";
    }
    const reconnectMaterial = await identityCredentialStore.loadForReconnect(
      window.location.origin,
    );
    return {
      before,
      kind,
      phase: session.currentSessionIdentityStatus().phase,
      reconnectMaterial,
    };
  });

  expect(rejected.kind).toBe("identity-lost");
  expect(rejected.phase).toBe("identity-lost");
  expect(rejected.reconnectMaterial.pending_rotation).not.toBeNull();
  await page.getByRole("button", { name: "重新连接设备身份" }).click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-session-identity",
    "ready",
  );
  const restored = await page.evaluate(async () => {
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    return identityCredentialStore.loadOrCreate(window.location.origin);
  });

  const pending = rejected.reconnectMaterial.pending_rotation;
  expect(pending).not.toBeNull();
  expect(renewCredentials).toEqual([
    pending?.new_device_credential,
    pending?.current_device_credential,
  ]);
  expect(restored.device_secret).toBe(rejected.before.device_secret);
  expect(restored.pending_rotation).toBeNull();
});

test("confirmed 409 stops automatic renewal and explicit reconnect reuses the same owner", async ({
  page,
}) => {
  let sessionRequired = false;
  let enrollCalls = 0;
  let enrolledSecret = "";
  let renewMode: "conflict" | "ok" = "conflict";
  const renewCredentials: string[] = [];
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    enrollCalls += 1;
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_secret?: string;
    };
    enrolledSecret = body.device_secret ?? "";
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        token: "owner-before-conflict",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      device_credential?: string;
    };
    renewCredentials.push(body.device_credential ?? "");
    if (renewMode === "conflict") {
      return route.fulfill({ status: 409, body: "identity_lost" });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: "same-owner-reconnected",
        expires_at: "2099-01-01T00:00:00Z",
      }),
    });
  });

  await page.goto("/");
  sessionRequired = true;
  const lost = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    session.resetSessionForTest();
    await session.ensureServerSession();
    const before = await identityCredentialStore.loadOrCreate(
      window.location.origin,
    );
    let kind = "";
    try {
      await session.ensureServerSession(true);
    } catch (error) {
      kind = (error as { kind?: string }).kind ?? "";
    }
    let automaticLoadKind = "";
    try {
      await identityCredentialStore.loadOrCreate(window.location.origin);
    } catch (error) {
      automaticLoadKind = (error as { kind?: string }).kind ?? "";
    }
    return {
      before,
      kind,
      automaticLoadKind,
      phase: session.currentSessionIdentityStatus().phase,
    };
  });

  expect(lost.kind).toBe("identity-lost");
  expect(lost.automaticLoadKind).toBe("identity-lost");
  expect(lost.phase).toBe("identity-lost");
  const reconnect = page.getByRole("button", { name: "重新连接设备身份" });
  await expect(reconnect).toBeVisible();
  await expect(reconnect).toContainText("重新连接");

  renewMode = "ok";
  await reconnect.click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-session-identity",
    "ready",
  );
  const restored = await page.evaluate(async () => {
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    return identityCredentialStore.loadOrCreate(window.location.origin);
  });

  expect(enrollCalls).toBe(1);
  expect(renewCredentials).toEqual([enrolledSecret, enrolledSecret]);
  expect(restored.enrollment_id).toBe(lost.before.enrollment_id);
  expect(restored.device_secret).toBe(lost.before.device_secret);
  expect(restored.enrollment_confirmed).toBe(true);
});

test("enrollment 409 marks identity lost and never silently enrolls a replacement", async ({
  page,
}) => {
  let sessionRequired = false;
  let enrollCalls = 0;
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: bootstrapBody(sessionRequired),
    }),
  );
  await page.route(/\/(api\/)?session\/enroll$/, (route) => {
    enrollCalls += 1;
    return route.fulfill({ status: 409, body: "enrollment_conflict" });
  });
  await page.route(/\/(api\/)?session\/renew$/, (route) =>
    route.fulfill({ status: 401, body: "identity_lost" }),
  );

  await page.goto("/");
  sessionRequired = true;
  const result = await page.evaluate(async () => {
    const session = await import("/src/session.ts");
    session.resetSessionForTest();
    const kinds: string[] = [];
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await session.ensureServerSession();
      } catch (error) {
        kinds.push((error as { kind?: string }).kind ?? "");
      }
    }
    return {
      kinds,
      phase: session.currentSessionIdentityStatus().phase,
    };
  });

  expect(result.kinds).toEqual(["identity-lost", "identity-lost"]);
  expect(result.phase).toBe("identity-lost");
  expect(enrollCalls).toBe(1);
  await expect(
    page.getByRole("button", { name: "重新连接设备身份" }),
  ).toBeVisible();
});

test("switching backend origin invalidates cached sessions and uses a distinct credential", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    const originalFetch = window.fetch;
    const enrollments: Array<{ origin: string; secret: string }> = [];
    const renewals: Array<{ origin: string; secret: string }> = [];
    let protectedAuthorization = "";
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const url = new URL(raw, window.location.href);
      if (url.hostname !== "identity-a.example" && url.hostname !== "identity-b.example") {
        return originalFetch(input, init);
      }
      if (url.pathname === "/bootstrap") {
        return new Response(JSON.stringify({
          schema_version: 1,
          api_version: "0.3",
          backend_version: "0.3.1-test",
          session_required: true,
          capabilities: { principal_sessions: true },
        }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.pathname === "/session/enroll") {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          device_secret?: string;
        };
        enrollments.push({
          origin: url.origin,
          secret: body.device_secret ?? "",
        });
        const suffix = url.hostname.startsWith("identity-a") ? "a" : "b";
        return new Response(
          JSON.stringify({
            token: `token-${suffix}`,
            expires_at: "2099-01-01T00:00:00Z",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.pathname === "/session/renew") {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          device_credential?: string;
        };
        renewals.push({
          origin: url.origin,
          secret: body.device_credential ?? "",
        });
        return new Response("unexpected renewal", { status: 500 });
      }
      if (url.pathname === "/protected") {
        protectedAuthorization = new Headers(init?.headers).get("Authorization") ?? "";
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("not found", { status: 404 });
    };

    runtime.setStoredBackendBase("https://identity-a.example");
    session.resetSessionForTest();
    const tokenA = await session.ensureServerSession();
    const identityA = await identityCredentialStore.loadOrCreate(
      "https://identity-a.example",
    );

    runtime.setStoredBackendBase("https://identity-b.example");
    const protectedResponse = await session.authenticatedFetch(
      "https://identity-b.example/protected",
    );
    const identityB = await identityCredentialStore.loadOrCreate(
      "https://identity-b.example",
    );
    runtime.setStoredBackendBase("");
    window.fetch = originalFetch;
    return {
      tokenA,
      protectedStatus: protectedResponse.status,
      protectedAuthorization,
      identityA,
      identityB,
      enrollments,
      renewals,
    };
  });

  expect(result.tokenA).toBe("token-a");
  expect(result.protectedStatus).toBe(200);
  expect(result.protectedAuthorization).toBe("Bearer token-b");
  expect(result.enrollments).toHaveLength(2);
  expect(result.enrollments.map((entry) => entry.origin)).toEqual([
    "https://identity-a.example",
    "https://identity-b.example",
  ]);
  expect(result.renewals).toEqual([]);
  expect(result.identityB.enrollment_id).not.toBe(result.identityA.enrollment_id);
  expect(result.identityB.device_secret).not.toBe(result.identityA.device_secret);
  expect(result.enrollments[0].secret).toBe(result.identityA.device_secret);
  expect(result.enrollments[1].secret).toBe(result.identityB.device_secret);
});

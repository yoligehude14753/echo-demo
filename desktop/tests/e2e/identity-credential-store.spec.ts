import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("browser identity is honest memory-only, origin-bound, and fail-closed", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    const capability = await identityCredentialStore.capability();
    const first = await identityCredentialStore.loadOrCreate(
      "https://IDENTITY.example:443/api",
    );
    const same = await identityCredentialStore.loadOrCreate(
      "https://identity.example/other-path",
    );
    await identityCredentialStore.confirmEnrollment(first.origin);
    const confirmed = await identityCredentialStore.loadOrCreate(first.origin);
    const other = await identityCredentialStore.loadOrCreate(
      "https://other.example/api",
    );

    const pending = await identityCredentialStore.beginRotation(first.origin);
    const pendingAgain = await identityCredentialStore.beginRotation(first.origin);
    const withPending = await identityCredentialStore.loadOrCreate(first.origin);
    await identityCredentialStore.commitRotation(first.origin, pending.rotation_id);
    const rotated = await identityCredentialStore.loadOrCreate(first.origin);

    await identityCredentialStore.markIdentityLost(first.origin);
    let lostKind = "";
    try {
      await identityCredentialStore.loadOrCreate(first.origin);
    } catch (error) {
      lostKind = (error as { kind?: string }).kind ?? "";
    }

    const persistedText = JSON.stringify({
      localStorage: { ...window.localStorage },
      sessionStorage: { ...window.sessionStorage },
    });
    await identityCredentialStore.clear(first.origin);
    const reset = await identityCredentialStore.loadOrCreate(first.origin);
    return {
      capability,
      dataset: {
        persistence: document.documentElement.dataset.identityPersistence,
        durable: document.documentElement.dataset.identityDurable,
        originBound: document.documentElement.dataset.identityOriginBound,
      },
      first,
      same,
      confirmed,
      other,
      pending,
      pendingAgain,
      withPending,
      rotated,
      lostKind,
      persistedText,
      reset,
    };
  });

  expect(result.capability).toEqual({
    runtime: "browser-memory",
    persistence: "memory-only",
    durable: false,
    originBound: true,
    atomicRotation: true,
    keyNonExportable: null,
    hardwareBacked: null,
  });
  expect(result.dataset).toEqual({
    persistence: "memory-only",
    durable: "false",
    originBound: "true",
  });
  expect(result.first.origin).toBe("https://identity.example");
  expect(result.first.created).toBe(true);
  expect(result.first.enrollment_confirmed).toBe(false);
  expect(result.same.created).toBe(false);
  expect(result.same.enrollment_confirmed).toBe(false);
  expect(result.same.enrollment_id).toBe(result.first.enrollment_id);
  expect(result.same.device_secret).toBe(result.first.device_secret);
  expect(result.confirmed.enrollment_confirmed).toBe(true);
  expect(result.confirmed.enrollment_id).toBe(result.first.enrollment_id);
  expect(result.confirmed.device_secret).toBe(result.first.device_secret);
  expect(result.other.enrollment_id).not.toBe(result.first.enrollment_id);
  expect(result.other.device_secret).not.toBe(result.first.device_secret);
  expect(result.pendingAgain).toEqual(result.pending);
  expect(result.withPending.pending_rotation).toEqual(result.pending);
  expect(result.rotated.device_secret).toBe(result.pending.new_device_credential);
  expect(result.rotated.device_secret).not.toBe(result.first.device_secret);
  expect(result.rotated.pending_rotation).toBeNull();
  expect(result.lostKind).toBe("identity-lost");
  expect(result.persistedText).not.toContain(result.first.enrollment_id);
  expect(result.persistedText).not.toContain(result.first.device_secret);
  expect(result.persistedText).not.toContain(result.rotated.device_secret);
  expect(result.reset.created).toBe(true);
  expect(result.reset.enrollment_id).not.toBe(result.first.enrollment_id);
  expect(result.reset.device_secret).not.toBe(result.rotated.device_secret);
});

test("browser reload discards the temporary owner instead of pretending durability", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");
  const before = await page.evaluate(async () => {
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    return identityCredentialStore.loadOrCreate("https://reload.example");
  });

  await page.reload();
  const after = await page.evaluate(async () => {
    const { identityCredentialStore } = await import(
      "/src/identityCredentialStore.ts"
    );
    const capability = await identityCredentialStore.capability();
    const identity = await identityCredentialStore.loadOrCreate(
      "https://reload.example",
    );
    return { capability, identity };
  });

  expect(after.capability.durable).toBe(false);
  expect(after.capability.persistence).toBe("memory-only");
  expect(after.identity.created).toBe(true);
  expect(after.identity.enrollment_id).not.toBe(before.enrollment_id);
  expect(after.identity.device_secret).not.toBe(before.device_secret);
});

test("browser exposes the temporary identity limitation in the product UI", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const status = page.getByTestId("identity-status-temporary");
  await expect(status).toBeVisible();
  await expect(status).toContainText("临时身份");
  await status.hover();
  await expect(
    page.getByText("刷新或关闭页面后会建立新的独立身份", { exact: false }),
  ).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute(
    "data-identity-persistence",
    "memory-only",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-identity-durable",
    "false",
  );
});

test("Electron capability is never mislabeled as browser memory-only", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");

  await expect(page.locator("html")).toHaveAttribute(
    "data-identity-persistence",
    "secure-device",
  );
  await expect(page.locator("html")).toHaveAttribute(
    "data-identity-durable",
    "true",
  );
  await expect(page.getByTestId("identity-status-temporary")).toHaveCount(0);
});

test("Electron identity recovery errors are visible without exposing internals", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        throw new Error("safeStorage unavailable internal-detail");
      },
      renewPublicSession: async () => {
        throw new Error("safeStorage unavailable internal-detail");
      },
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    }),
  );

  await page.goto("/");
  const status = page.getByTestId("identity-status-error");
  await expect(status).toBeVisible();
  await expect(status).toContainText("安全身份存储不可用");
  await expect(page.locator("html")).toHaveAttribute(
    "data-identity-persistence",
    "secure-device",
  );
  await expect(page.getByText("internal-detail", { exact: false })).toHaveCount(0);
});

test("transient device identity errors expose a working retry action", async ({ page }) => {
  await page.addInitScript(() => {
    window.echo = {
      ...(window.echo ?? {}),
      isElectron: true,
      ensurePublicSession: async () => {
        throw new Error("temporary secure store failure");
      },
      renewPublicSession: async () => ({
        token: "retry-session-token",
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      }),
    };
  });
  await installEchoMock(page, { skipPaths: ["/bootstrap"] });
  await page.route(/\/(api\/)?bootstrap$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    }),
  );

  await page.goto("/");
  const retry = page.getByRole("button", { name: "重试设备身份连接" });
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(page.getByTestId("identity-status-error")).toHaveCount(0);
  await expect(page.locator("html")).toHaveAttribute("data-session-identity", "ready");
});

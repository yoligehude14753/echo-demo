import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("backend URL policy allows explicit private HTTP but rejects public cleartext", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const accepted = [
      "http://localhost:8769",
      "http://127.0.0.1:8769",
      "http://10.1.2.3:8769",
      "http://172.16.0.1:8769",
      "http://172.31.255.254:8769",
      "http://192.168.4.5:8769",
      "http://169.254.10.20:8769",
      "http://[::1]:8769",
      "http://[fd12:3456::1]:8769",
      "http://[fe80::1234]:8769",
      "https://public.example:443",
    ].map((value) => runtime.normalizeBackendBase(value));
    const rejected = [
      "http://public.example:8769",
      "http://8.8.8.8:8769",
      "http://172.32.0.1:8769",
      "http://192.0.2.1:8769",
      "http://[2001:db8::1]:8769",
      "http://user:pass@10.0.0.2:8769",
      "http://10.0.0.2:8769/api",
      "ftp://10.0.0.2:8769",
    ].map((value) => {
      try {
        runtime.normalizeBackendBase(value);
        return null;
      } catch (error) {
        return error instanceof Error ? error.message : String(error);
      }
    });
    return { accepted, rejected };
  });

  expect(result.accepted).toEqual([
    "http://localhost:8769",
    "http://127.0.0.1:8769",
    "http://10.1.2.3:8769",
    "http://172.16.0.1:8769",
    "http://172.31.255.254:8769",
    "http://192.168.4.5:8769",
    "http://169.254.10.20:8769",
    "http://[::1]:8769",
    "http://[fd12:3456::1]:8769",
    "http://[fe80::1234]:8769",
    "https://public.example",
  ]);
  expect(result.rejected.every((message) => typeof message === "string")).toBe(true);
});

test("settings rejects public HTTP and requires explicit confirmation for private HTTP", async ({
  page,
}) => {
  await installEchoMock(page);
  await page.goto("/");
  await page.getByTestId("open-settings").click();

  const input = page.getByTestId("mobile-backend-base");
  await input.fill("http://public.example:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  await expect(page.getByText("公网主机必须使用 HTTPS", { exact: false })).toBeVisible();
  await expect(input).toHaveValue("http://public.example:8769");
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBeNull();

  await input.fill("http://192.168.8.20:8769");
  await page.getByTestId("save-mobile-backend-base").click();
  const dialog = page.getByRole("dialog", { name: "确认使用局域网明文连接？" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("需要设备身份的服务会拒绝通过 HTTP 发送凭证");
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBeNull();
  await dialog.getByRole("button", { name: "确认仅用于可信局域网" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => window.localStorage.getItem("echodesk.mobileBackendBase")),
    )
    .toBe("http://192.168.8.20:8769");
});

test("session-required private HTTP fails before any device secret fetch", async ({ page }) => {
  let bootstrapCalls = 0;
  let identityCalls = 0;
  await installEchoMock(page, {
    isElectron: false,
    skipPaths: ["/bootstrap", "/session/enroll", "/session/renew"],
  });
  await page.route("http://10.20.30.40:8769/bootstrap", (route) => {
    bootstrapCalls += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        schema_version: 1,
        api_version: "0.3",
        backend_version: "0.3.1-test",
        session_required: true,
        capabilities: { principal_sessions: true },
      }),
    });
  });
  await page.route(/http:\/\/10\.20\.30\.40:8769\/session\/(?:enroll|renew)$/, (route) => {
    identityCalls += 1;
    return route.fulfill({ status: 500, body: "must not be called" });
  });

  await page.goto("/");
  const result = await page.evaluate(async () => {
    const runtime = await import("/src/runtime.ts");
    const session = await import("/src/session.ts");
    runtime.setStoredBackendBase("http://10.20.30.40:8769");
    session.resetSessionForTest();
    try {
      await session.ensureServerSession();
      return { kind: "", message: "" };
    } catch (error) {
      return {
        kind: (error as { kind?: string }).kind ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });

  expect(bootstrapCalls).toBe(1);
  expect(identityCalls).toBe(0);
  expect(result.kind).toBe("invalid-origin");
  expect(result.message).toContain("HTTPS");
});

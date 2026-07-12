import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

test("renderer credential rotation classification matches the Electron contract", async ({
  page,
}) => {
  await installEchoMock(page, { isElectron: false });
  await page.goto("/");

  const matrix = await page.evaluate(async () => {
    const { classifyCredentialRotationStatus } = await import("/src/session.ts");
    return Object.fromEntries(
      [400, 401, 403, 404, 408, 409, 413, 415, 422, 425, 429, 500, 503].map(
        (status) => [status, classifyCredentialRotationStatus(status)],
      ),
    );
  });

  expect(matrix).toEqual({
    400: "definitive-rejection",
    401: "identity-lost",
    403: "ambiguous",
    404: "ambiguous",
    408: "ambiguous",
    409: "identity-lost",
    413: "definitive-rejection",
    415: "definitive-rejection",
    422: "definitive-rejection",
    425: "ambiguous",
    429: "ambiguous",
    500: "ambiguous",
    503: "ambiguous",
  });
});

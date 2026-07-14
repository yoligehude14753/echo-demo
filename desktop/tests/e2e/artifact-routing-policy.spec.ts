import { expect, test } from "@playwright/test";
import { installEchoMock } from "./_mock";

type RoutingFixture = {
  runtimeMode: "release" | "development" | "diagnostic";
  principalMode: "local" | "public";
  role: "public_service" | "local_dev_diagnostic" | "paired_hub_sync_gateway";
  source: string;
  schemaVersion: number;
  backendBase: string;
  publicServiceEndpoint: string;
  pairedHubSyncGatewayEndpoint: null;
  localDevDiagnosticEndpoint: string | null;
};

async function installRoutingFixture(
  page: Parameters<typeof installEchoMock>[0],
  routing: RoutingFixture,
  backendHost = routing.backendBase,
): Promise<void> {
  await page.addInitScript(
    ({ backendHost: host, routing: fixture }) => {
      window.echo = {
        ...(window.echo ?? {}),
        isElectron: true,
        backendHost: host,
        backendRouting: fixture,
      };
    },
    { backendHost, routing },
  );
  await installEchoMock(page, { isElectron: true });
}

test("packaged public artifact URL uses only public_service snapshot", async ({
  page,
}) => {
  const routing: RoutingFixture = {
    runtimeMode: "release",
    principalMode: "public",
    role: "public_service",
    source: "release-config",
    schemaVersion: 2,
    backendBase: "https://public.example.test",
    publicServiceEndpoint: "https://public.example.test",
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: null,
  };
  await installRoutingFixture(page, routing);
  await page.goto("/");

  const href = await page.evaluate(async () => {
    const { artifactDownloadUrl } = await import("/src/api.ts");
    return artifactDownloadUrl("artifact-123");
  });
  expect(href).toBe("https://public.example.test/artifacts/artifact-123/download");
});

test("missing packaged snapshot and legacy preload never fall back to localhost", async ({
  page,
}) => {
  const routing: RoutingFixture = {
    runtimeMode: "release",
    principalMode: "public",
    role: "public_service",
    source: "release-config",
    schemaVersion: 2,
    backendBase: "",
    publicServiceEndpoint: "https://public.example.test",
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: null,
  };
  await installRoutingFixture(page, routing, "");
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const { artifactDownloadUrl } = await import("/src/api.ts");
    try {
      artifactDownloadUrl("artifact-123");
      return { name: "", code: "" };
    } catch (error) {
      return {
        name: error instanceof Error ? error.name : "",
        code: (error as { code?: string }).code ?? "",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  });
  expect(result).toMatchObject({
    name: "BackendBasePolicyError",
    code: "artifact_backend_snapshot_missing",
  });
  expect(result.message).not.toContain("127.0.0.1");
});

test("explicit diagnostic local artifact URL is allowed only for local role", async ({
  page,
}) => {
  const routing: RoutingFixture = {
    runtimeMode: "diagnostic",
    principalMode: "local",
    role: "local_dev_diagnostic",
    source: "explicit-local-endpoint",
    schemaVersion: 2,
    backendBase: "http://127.0.0.1:19001",
    publicServiceEndpoint: "https://public.example.test",
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: "http://127.0.0.1:19001",
  };
  await installRoutingFixture(page, routing);
  await page.goto("/");

  const href = await page.evaluate(async () => {
    const { artifactDownloadUrl } = await import("/src/api.ts");
    return artifactDownloadUrl("artifact-123");
  });
  expect(href).toBe("http://127.0.0.1:19001/artifacts/artifact-123/download");
});

test("paired Hub role and path escapes are rejected", async ({ page }) => {
  const routing: RoutingFixture = {
    runtimeMode: "diagnostic",
    principalMode: "public",
    role: "paired_hub_sync_gateway",
    source: "invalid-hub-role",
    schemaVersion: 2,
    backendBase: "https://hub.example.test",
    publicServiceEndpoint: "https://public.example.test",
    pairedHubSyncGatewayEndpoint: null,
    localDevDiagnosticEndpoint: null,
  };
  await installRoutingFixture(page, routing);
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const { artifactDownloadUrl } = await import("/src/api.ts");
    const read = (value: string) => {
      try {
        artifactDownloadUrl(value);
        return { name: "", code: "" };
      } catch (error) {
        return {
          name: error instanceof Error ? error.name : "",
          code: (error as { code?: string }).code ?? "",
        };
      }
    };
    return {
      hub: read("artifact-123"),
      absolute: read("https://outside.example/artifact"),
      crossPath: read("../other-origin"),
    };
  });
  expect(result.hub).toEqual({
    name: "BackendBasePolicyError",
    code: "artifact_hub_role_forbidden",
  });
  expect(result.absolute).toEqual({
    name: "BackendBasePolicyError",
    code: "artifact_path_invalid",
  });
  expect(result.crossPath).toEqual({
    name: "BackendBasePolicyError",
    code: "artifact_path_invalid",
  });
});

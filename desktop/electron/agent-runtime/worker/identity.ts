import { randomUUID } from "node:crypto";
import {
  assertSameBuildIdentity,
  sameBuildIdentity,
  validateBuildIdentity,
  type KernelBuildIdentity,
} from "../../../agent-kernel/core/index.ts";

export const RUNTIME_MANIFEST_SCHEMA_VERSION = 1 as const;

export type RuntimeContractVersions = {
  kernelApi: 1;
  workerIpc: 1;
  modelRuntime: 1;
  grantSnapshot: 1;
  checkpoint: 1;
  event: 1;
};

export type RuntimeManifest = {
  schemaVersion: 1;
  manifestId: string;
  buildIdentity: KernelBuildIdentity;
  contracts: RuntimeContractVersions;
};

const CONTRACT_VERSIONS: RuntimeContractVersions = Object.freeze({
  kernelApi: 1,
  workerIpc: 1,
  modelRuntime: 1,
  grantSnapshot: 1,
  checkpoint: 1,
  event: 1,
});

function assertManifestId(manifestId: string): void {
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(manifestId)) {
    throw new Error("runtime manifest id is invalid");
  }
}

export function createRuntimeManifest(buildIdentity: KernelBuildIdentity, manifestId: string): RuntimeManifest {
  validateBuildIdentity(buildIdentity);
  assertManifestId(manifestId);
  return Object.freeze({
    schemaVersion: RUNTIME_MANIFEST_SCHEMA_VERSION,
    manifestId,
    buildIdentity: Object.freeze({ ...buildIdentity, runtimeFingerprint: Object.freeze({ ...buildIdentity.runtimeFingerprint }) }),
    contracts: CONTRACT_VERSIONS,
  });
}

export function validateRuntimeManifest(manifest: RuntimeManifest): RuntimeManifest {
  if (manifest.schemaVersion !== RUNTIME_MANIFEST_SCHEMA_VERSION) throw new Error("runtime manifest schema version is unsupported");
  assertManifestId(manifest.manifestId);
  validateBuildIdentity(manifest.buildIdentity);
  const contracts = manifest.contracts;
  if (!contracts || Object.values(contracts).some((version) => version !== 1)) {
    throw new Error("runtime manifest contract version is unsupported");
  }
  return manifest;
}

export function assertRuntimeManifestMatches(expected: RuntimeManifest, actual: RuntimeManifest): void {
  validateRuntimeManifest(expected);
  validateRuntimeManifest(actual);
  if (expected.manifestId !== actual.manifestId || !sameBuildIdentity(expected.buildIdentity, actual.buildIdentity)) {
    throw new Error("runtime manifest identity mismatch");
  }
}

export function assertWorkerBuildIdentity(expected: KernelBuildIdentity, actual: KernelBuildIdentity): void {
  validateBuildIdentity(expected);
  validateBuildIdentity(actual);
  assertSameBuildIdentity(expected, actual);
}

/** Runtime event IDs are delivery-local identifiers; durable seq/hash belongs to B10. */
export function newRuntimeEventId(): string {
  return `runtime-${randomUUID()}`;
}

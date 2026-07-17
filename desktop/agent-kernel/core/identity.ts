import { KernelError } from "./errors.ts";
import type { KernelBuildIdentity } from "./types.ts";

const IDENTITY_FIELDS = [
  "schemaVersion",
  "kernelApiVersion",
  "workerProtocolVersion",
  "modelSchemaVersion",
  "grantSchemaVersion",
  "checkpointSchemaVersion",
  "eventSchemaVersion",
  "buildId",
  "sourceSnapshotId",
  "sourceManifestSha256",
  "echoBaselineSha",
  "runtimeFingerprint",
] as const;

export function validateBuildIdentity(identity: KernelBuildIdentity): KernelBuildIdentity {
  if (identity.schemaVersion !== 1 || identity.kernelApiVersion !== 1 || identity.workerProtocolVersion !== 1) {
    throw new KernelError("RUNTIME_PROTOCOL_MISMATCH", "kernel identity protocol version is unsupported");
  }
  if (
    identity.modelSchemaVersion !== 1 ||
    identity.grantSchemaVersion !== 1 ||
    identity.checkpointSchemaVersion !== 1 ||
    identity.eventSchemaVersion !== 1
  ) {
    throw new KernelError("RUNTIME_PROTOCOL_MISMATCH", "kernel identity schema version is unsupported");
  }
  if (!identity.buildId || !/^[A-Za-z0-9._:-]{1,128}$/.test(identity.buildId)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "kernel build identity is invalid");
  }
  if (!/^sha256:[a-f0-9]{64}$/i.test(identity.sourceSnapshotId)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "kernel source snapshot identity is invalid");
  }
  if (!/^[a-f0-9]{64}$/i.test(identity.sourceManifestSha256)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "kernel source manifest identity is invalid");
  }
  if (!/^[a-f0-9]{40}$/i.test(identity.echoBaselineSha)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "Echo compatibility baseline identity is invalid");
  }
  const runtime = identity.runtimeFingerprint;
  if (!runtime || Object.values(runtime).some((value) => typeof value !== "string" || value.length === 0)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "runtime fingerprint identity is invalid");
  }
  return Object.freeze({ ...identity });
}

export function sameBuildIdentity(left: KernelBuildIdentity, right: KernelBuildIdentity): boolean {
  return IDENTITY_FIELDS.every((field) => {
    if (field === "runtimeFingerprint") {
      return Object.keys(left.runtimeFingerprint).every(
        (key) => left.runtimeFingerprint[key as keyof typeof left.runtimeFingerprint] === right.runtimeFingerprint[key as keyof typeof right.runtimeFingerprint],
      );
    }
    return left[field] === right[field];
  });
}

export function assertSameBuildIdentity(expected: KernelBuildIdentity, actual: KernelBuildIdentity): void {
  if (!sameBuildIdentity(expected, actual)) {
    throw new KernelError("RUNTIME_BUILD_MISMATCH", "kernel startup identity mismatch", {
      expectedBuildId: expected.buildId,
      actualBuildId: actual.buildId,
      expectedManifest: expected.sourceManifestSha256,
      actualManifest: actual.sourceManifestSha256,
    });
  }
}

import { Capacitor, registerPlugin } from "@capacitor/core";

export const IDENTITY_CAPABILITY_EVENT = "echodesk:identity-capability";

export type IdentityPersistence = "secure-device" | "memory-only";
export type IdentityRuntime =
  | "android-keystore"
  | "electron-keychain"
  | "browser-memory";
export type IdentityCredentialErrorKind =
  | "identity-lost"
  | "identity-missing"
  | "invalid-origin"
  | "rotation-mismatch"
  | "secure-store-unavailable";

export interface IdentityCredentialCapability {
  runtime: IdentityRuntime;
  persistence: IdentityPersistence;
  durable: boolean;
  originBound: true;
  atomicRotation: true;
  keyNonExportable: boolean | null;
  hardwareBacked: boolean | null;
}

export interface DeviceIdentityMaterial {
  origin: string;
  enrollment_id: string;
  device_secret: string;
  created: boolean;
  enrollment_confirmed: boolean;
  pending_rotation: DeviceCredentialRotation | null;
}

export interface DeviceCredentialRotation {
  origin: string;
  rotation_id: string;
  current_device_credential: string;
  new_device_credential: string;
}

export class IdentityCredentialStoreError extends Error {
  constructor(
    message: string,
    public readonly kind: IdentityCredentialErrorKind,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "IdentityCredentialStoreError";
  }
}

interface NativeCapabilityResult {
  runtime: "android-keystore";
  persistence: "secure-device";
  durable: boolean;
  originBound: boolean;
  atomicRotation: boolean;
  keyNonExportable: boolean;
  hardwareBacked: boolean;
}

interface NativeIdentityResult {
  origin: string;
  enrollmentId: string;
  deviceSecret: string;
  created: boolean;
  enrollmentConfirmed: boolean;
  pendingRotationId: string | null;
  pendingDeviceSecret: string | null;
}

interface NativeRotationResult {
  origin: string;
  rotationId: string;
  currentDeviceSecret: string;
  newDeviceSecret: string;
}

interface EchoIdentityPlugin {
  capabilities(): Promise<NativeCapabilityResult>;
  loadOrCreate(options: { origin: string }): Promise<NativeIdentityResult>;
  loadForReconnect(options: { origin: string }): Promise<NativeIdentityResult>;
  confirmEnrollment(options: { origin: string }): Promise<{ ok: boolean }>;
  beginRotation(options: { origin: string }): Promise<NativeRotationResult>;
  commitRotation(options: { origin: string; rotationId: string }): Promise<{ ok: boolean }>;
  abortRotation(options: { origin: string; rotationId: string }): Promise<{ ok: boolean }>;
  markIdentityLost(options: { origin: string }): Promise<{ ok: boolean }>;
  restoreIdentity(options: { origin: string }): Promise<{ ok: boolean }>;
  clear(options: { origin: string }): Promise<{ ok: boolean }>;
}

interface MemoryIdentity {
  enrollmentId: string;
  deviceSecret: string;
  enrollmentConfirmed: boolean;
  lost: boolean;
  pendingRotationId: string | null;
  pendingDeviceSecret: string | null;
}

const EchoIdentity = registerPlugin<EchoIdentityPlugin>("EchoIdentity");
const memoryIdentities = new Map<string, MemoryIdentity>();

const BROWSER_CAPABILITY: IdentityCredentialCapability = Object.freeze({
  runtime: "browser-memory",
  persistence: "memory-only",
  durable: false,
  originBound: true,
  atomicRotation: true,
  keyNonExportable: null,
  hardwareBacked: null,
});

const ELECTRON_CAPABILITY: IdentityCredentialCapability = Object.freeze({
  runtime: "electron-keychain",
  persistence: "secure-device",
  durable: true,
  originBound: true,
  atomicRotation: true,
  keyNonExportable: null,
  hardwareBacked: null,
});

function isNativeRuntime(): boolean {
  return Capacitor.isNativePlatform();
}

function isElectronRuntime(): boolean {
  return typeof window !== "undefined" && window.echo?.isElectron === true;
}

function electronManagedError(): IdentityCredentialStoreError {
  return new IdentityCredentialStoreError(
    "Electron 设备身份只能由受信任主进程凭证库管理",
    "secure-store-unavailable",
  );
}

export function normalizeIdentityOrigin(raw: string): string {
  let value: URL;
  try {
    const base = typeof window === "undefined" ? undefined : window.location.href;
    value = base ? new URL(raw, base) : new URL(raw);
  } catch (error) {
    throw new IdentityCredentialStoreError(
      "后端地址不是有效 URL",
      "invalid-origin",
      { cause: error },
    );
  }
  if (
    value.protocol !== "https:" ||
    value.username !== "" ||
    value.password !== ""
  ) {
    throw new IdentityCredentialStoreError(
      "设备身份凭证只能绑定到不含用户信息的 HTTPS 后端 origin",
      "invalid-origin",
    );
  }
  return value.origin;
}

function publishCapability(capability: IdentityCredentialCapability): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.identityPersistence = capability.persistence;
    document.documentElement.dataset.identityDurable = String(capability.durable);
    document.documentElement.dataset.identityOriginBound = "true";
  }
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent<IdentityCredentialCapability>(IDENTITY_CAPABILITY_EVENT, {
        detail: capability,
      }),
    );
  }
}

function randomSecret(): string {
  if (typeof crypto === "undefined" || typeof crypto.getRandomValues !== "function") {
    throw new IdentityCredentialStoreError(
      "安全随机数不可用，不能创建临时设备身份",
      "secure-store-unavailable",
    );
  }
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function identityLost(cause?: unknown): IdentityCredentialStoreError {
  return new IdentityCredentialStoreError(
    "此后端的设备身份已失效，需要用户明确重置后才能创建新身份",
    "identity-lost",
    { cause },
  );
}

function identityMissing(cause?: unknown): IdentityCredentialStoreError {
  return new IdentityCredentialStoreError(
    "此后端尚未建立设备身份",
    "identity-missing",
    { cause },
  );
}

function normalizeNativeError(error: unknown): IdentityCredentialStoreError {
  if (error instanceof IdentityCredentialStoreError) return error;
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code?: unknown }).code ?? "")
      : "";
  if (code === "IDENTITY_LOST") return identityLost(error);
  if (code === "IDENTITY_MISSING") return identityMissing(error);
  if (code === "ROTATION_MISMATCH") {
    return new IdentityCredentialStoreError(
      "凭证轮换状态不匹配",
      "rotation-mismatch",
      { cause: error },
    );
  }
  if (code === "INVALID_ORIGIN") {
    return new IdentityCredentialStoreError(
      "后端 origin 无效",
      "invalid-origin",
      { cause: error },
    );
  }
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("rotation_mismatch")) {
    return new IdentityCredentialStoreError(
      "凭证轮换状态不匹配",
      "rotation-mismatch",
      { cause: error },
    );
  }
  return new IdentityCredentialStoreError(
    "Android 安全身份存储不可用",
    "secure-store-unavailable",
    { cause: error },
  );
}

function validateSecret(value: string, field: string): string {
  if (typeof value !== "string" || value.length < 32) {
    throw new IdentityCredentialStoreError(
      `Android 安全区返回了无效的 ${field}`,
      "secure-store-unavailable",
    );
  }
  return value;
}

function requireNativeAck(
  value: { ok?: boolean } | null | undefined,
  operation: string,
): void {
  if (value?.ok !== true) {
    throw new Error(`native identity ${operation} acknowledgement mismatch`);
  }
}

function nativeIdentityMaterial(
  value: NativeIdentityResult,
  origin: string,
): DeviceIdentityMaterial {
  if (value.origin !== origin) {
    throw new Error("native identity origin mismatch");
  }
  const hasPendingRotationId = value.pendingRotationId != null;
  const hasPendingDeviceSecret = value.pendingDeviceSecret != null;
  if (hasPendingRotationId !== hasPendingDeviceSecret) {
    throw new Error("native identity pending rotation contract mismatch");
  }
  return {
    origin,
    enrollment_id: validateSecret(value.enrollmentId, "enrollment id"),
    device_secret: validateSecret(value.deviceSecret, "device secret"),
    created: value.created === true,
    enrollment_confirmed: value.enrollmentConfirmed === true,
    pending_rotation:
      hasPendingRotationId && hasPendingDeviceSecret
        ? {
            origin,
            rotation_id: validateSecret(
              value.pendingRotationId as string,
              "pending rotation id",
            ),
            current_device_credential: validateSecret(
              value.deviceSecret,
              "current device credential",
            ),
            new_device_credential: validateSecret(
              value.pendingDeviceSecret as string,
              "pending device credential",
            ),
          }
        : null,
  };
}

function requireMemoryIdentity(origin: string): MemoryIdentity {
  const identity = memoryIdentities.get(origin);
  if (!identity) throw identityMissing();
  if (identity.lost) throw identityLost();
  return identity;
}

export const identityCredentialStore = {
  async capability(): Promise<IdentityCredentialCapability> {
    if (isElectronRuntime()) {
      publishCapability(ELECTRON_CAPABILITY);
      return ELECTRON_CAPABILITY;
    }
    if (!isNativeRuntime()) {
      publishCapability(BROWSER_CAPABILITY);
      return BROWSER_CAPABILITY;
    }
    try {
      const value = await EchoIdentity.capabilities();
      if (
        value.runtime !== "android-keystore" ||
        value.persistence !== "secure-device" ||
        value.durable !== true ||
        value.originBound !== true ||
        value.atomicRotation !== true ||
        value.keyNonExportable !== true
      ) {
        throw new Error("native identity capability contract mismatch");
      }
      const capability: IdentityCredentialCapability = {
        runtime: value.runtime,
        persistence: value.persistence,
        durable: true,
        originBound: true,
        atomicRotation: true,
        keyNonExportable: true,
        hardwareBacked: value.hardwareBacked === true,
      };
      publishCapability(capability);
      return capability;
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async loadOrCreate(rawOrigin: string): Promise<DeviceIdentityMaterial> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      let identity = memoryIdentities.get(origin);
      const created = !identity;
      if (!identity) {
        identity = {
          enrollmentId: randomSecret(),
          deviceSecret: randomSecret(),
          enrollmentConfirmed: false,
          lost: false,
          pendingRotationId: null,
          pendingDeviceSecret: null,
        };
        memoryIdentities.set(origin, identity);
      }
      if (identity.lost) throw identityLost();
      publishCapability(BROWSER_CAPABILITY);
      return {
        origin,
        enrollment_id: identity.enrollmentId,
        device_secret: identity.deviceSecret,
        created,
        enrollment_confirmed: identity.enrollmentConfirmed,
        pending_rotation:
          identity.pendingRotationId && identity.pendingDeviceSecret
            ? {
                origin,
                rotation_id: identity.pendingRotationId,
                current_device_credential: identity.deviceSecret,
                new_device_credential: identity.pendingDeviceSecret,
              }
            : null,
      };
    }
    try {
      const value = await EchoIdentity.loadOrCreate({ origin });
      return nativeIdentityMaterial(value, origin);
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async loadForReconnect(rawOrigin: string): Promise<DeviceIdentityMaterial> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = memoryIdentities.get(origin);
      if (!identity) throw identityMissing();
      publishCapability(BROWSER_CAPABILITY);
      return {
        origin,
        enrollment_id: identity.enrollmentId,
        device_secret: identity.deviceSecret,
        created: false,
        enrollment_confirmed: identity.enrollmentConfirmed,
        pending_rotation:
          identity.pendingRotationId && identity.pendingDeviceSecret
            ? {
                origin,
                rotation_id: identity.pendingRotationId,
                current_device_credential: identity.deviceSecret,
                new_device_credential: identity.pendingDeviceSecret,
              }
            : null,
      };
    }
    try {
      return nativeIdentityMaterial(
        await EchoIdentity.loadForReconnect({ origin }),
        origin,
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async confirmEnrollment(rawOrigin: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = requireMemoryIdentity(origin);
      identity.enrollmentConfirmed = true;
      return;
    }
    try {
      requireNativeAck(
        await EchoIdentity.confirmEnrollment({ origin }),
        "confirmEnrollment",
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async beginRotation(rawOrigin: string): Promise<DeviceCredentialRotation> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = requireMemoryIdentity(origin);
      if (!identity.pendingRotationId || !identity.pendingDeviceSecret) {
        identity.pendingRotationId = randomSecret();
        identity.pendingDeviceSecret = randomSecret();
      }
      return {
        origin,
        rotation_id: identity.pendingRotationId,
        current_device_credential: identity.deviceSecret,
        new_device_credential: identity.pendingDeviceSecret,
      };
    }
    try {
      const value = await EchoIdentity.beginRotation({ origin });
      if (value.origin !== origin) throw new Error("native identity origin mismatch");
      return {
        origin,
        rotation_id: validateSecret(value.rotationId, "rotation id"),
        current_device_credential: validateSecret(
          value.currentDeviceSecret,
          "current device credential",
        ),
        new_device_credential: validateSecret(
          value.newDeviceSecret,
          "new device credential",
        ),
      };
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async commitRotation(rawOrigin: string, rotationId: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = requireMemoryIdentity(origin);
      if (
        identity.pendingRotationId !== rotationId ||
        identity.pendingDeviceSecret === null
      ) {
        throw new IdentityCredentialStoreError(
          "凭证轮换状态不匹配",
          "rotation-mismatch",
        );
      }
      identity.deviceSecret = identity.pendingDeviceSecret;
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
      return;
    }
    try {
      requireNativeAck(
        await EchoIdentity.commitRotation({ origin, rotationId }),
        "commitRotation",
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async abortRotation(rawOrigin: string, rotationId: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = requireMemoryIdentity(origin);
      if (identity.pendingRotationId !== rotationId) {
        throw new IdentityCredentialStoreError(
          "凭证轮换状态不匹配",
          "rotation-mismatch",
        );
      }
      identity.pendingRotationId = null;
      identity.pendingDeviceSecret = null;
      return;
    }
    try {
      requireNativeAck(
        await EchoIdentity.abortRotation({ origin, rotationId }),
        "abortRotation",
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async markIdentityLost(rawOrigin: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = memoryIdentities.get(origin);
      if (!identity) throw identityMissing();
      identity.lost = true;
      return;
    }
    try {
      requireNativeAck(
        await EchoIdentity.markIdentityLost({ origin }),
        "markIdentityLost",
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async restoreIdentity(rawOrigin: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      const identity = memoryIdentities.get(origin);
      if (!identity) throw identityMissing();
      identity.lost = false;
      return;
    }
    try {
      requireNativeAck(
        await EchoIdentity.restoreIdentity({ origin }),
        "restoreIdentity",
      );
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },

  async clear(rawOrigin: string): Promise<void> {
    const origin = normalizeIdentityOrigin(rawOrigin);
    if (isElectronRuntime()) throw electronManagedError();
    if (!isNativeRuntime()) {
      memoryIdentities.delete(origin);
      return;
    }
    try {
      requireNativeAck(await EchoIdentity.clear({ origin }), "clear");
    } catch (error) {
      throw normalizeNativeError(error);
    }
  },
};

export function resetIdentityCredentialStoreForTest(): void {
  memoryIdentities.clear();
}

if (typeof window !== "undefined") {
  if (isElectronRuntime()) publishCapability(ELECTRON_CAPABILITY);
  else if (!isNativeRuntime()) publishCapability(BROWSER_CAPABILITY);
}

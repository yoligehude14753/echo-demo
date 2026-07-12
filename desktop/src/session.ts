import {
  apiUrl,
  backendBase,
  backendWsUrl,
  shouldHideSharedPublicHistory,
} from "@/runtime";
import {
  IdentityCredentialStoreError,
  identityCredentialStore,
  resetIdentityCredentialStoreForTest,
} from "@/identityCredentialStore";

const BOOTSTRAP_TIMEOUT_MS = 4_000;
const DEFAULT_API_TIMEOUT_MS = 180_000;
export const SESSION_IDENTITY_EVENT = "echodesk:session-identity";

export type SessionIdentityPhase =
  | "idle"
  | "establishing"
  | "ready"
  | "renewing"
  | "identity-lost"
  | "error";

export interface SessionIdentityStatus {
  phase: SessionIdentityPhase;
  message: string | null;
}

export interface BackendBootstrap {
  schema_version: number;
  api_version: string;
  backend_version?: string;
  session_required: boolean;
  capabilities: Record<string, unknown>;
}

export type BackendCompatibility = "unknown" | "compatible" | "legacy" | "unreachable";

export type ApiTransportErrorKind = "timeout" | "aborted" | "network" | "http";

export class ApiTransportError extends Error {
  constructor(
    message: string,
    public readonly kind: ApiTransportErrorKind,
    public readonly url: string,
    public readonly status: number | null = null,
    public readonly detail: string | null = null,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ApiTransportError";
  }
}

export interface ApiTransportOptions {
  timeoutMs?: number;
  /**
   * API helpers that intentionally branch on 404/403 can opt out and inspect the
   * Response themselves. Transport/network failures are always normalized.
   */
  throwHttpErrors?: boolean;
  /**
   * 仅用于 backend 明确声明为匿名的元数据端点（如 /healthz、/bootstrap）。
   * 开启后不会建立/续签 session，也不会发送 Authorization。
   */
  anonymous?: boolean;
}

let bootstrapPromise: Promise<BackendBootstrap | null> | null = null;
let bootstrapPromiseOrigin: string | null = null;
let sessionPromise: Promise<string | null> | null = null;
let sessionPromiseOrigin: string | null = null;
let forcedRenewPromise: Promise<string | null> | null = null;
let forcedRenewPromiseOrigin: string | null = null;
let currentSession: {
  token: string;
  expiresAt: string | null;
  scopeKey: string;
  origin: string;
} | null = null;
let activeBackendOrigin: string | null = null;
let compatibility: BackendCompatibility = "unknown";
let identityStatus: SessionIdentityStatus = { phase: "idle", message: null };
let rendererIdentityTail: Promise<void> = Promise.resolve();

interface IssuedSessionResponse {
  token?: string | null;
  expires_at?: string | null;
  credential_expires_at?: string | null;
  principal?: Record<string, unknown>;
}

class DeviceSessionRequestError extends Error {
  constructor(
    public readonly endpoint: string,
    public readonly status: number,
  ) {
    super(`${endpoint} ${status}`);
    this.name = "DeviceSessionRequestError";
  }
}

export type CredentialRotationOutcome =
  | "identity-lost"
  | "definitive-rejection"
  | "ambiguous";

const DEFINITIVE_ROTATION_REJECTION_STATUSES = new Set([400, 413, 415, 422]);
const IDENTITY_ROTATION_REJECTION_STATUSES = new Set([401, 409]);

/** Keep this matrix identical to electron/public-identity-session.cjs. */
export function classifyCredentialRotationStatus(
  status: number,
): CredentialRotationOutcome {
  if (IDENTITY_ROTATION_REJECTION_STATUSES.has(status)) return "identity-lost";
  if (DEFINITIVE_ROTATION_REJECTION_STATUSES.has(status)) {
    return "definitive-rejection";
  }
  return "ambiguous";
}

function publishIdentityStatus(
  phase: SessionIdentityPhase,
  message: string | null = null,
): void {
  identityStatus = { phase, message };
  if (typeof document !== "undefined") {
    document.documentElement.dataset.sessionIdentity = phase;
  }
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent<SessionIdentityStatus>(SESSION_IDENTITY_EVENT, {
        detail: identityStatus,
      }),
    );
  }
}

export function currentSessionIdentityStatus(): SessionIdentityStatus {
  return identityStatus;
}

export function isIdentityLostError(
  error: unknown,
): error is IdentityCredentialStoreError {
  return (
    error instanceof IdentityCredentialStoreError &&
    error.kind === "identity-lost"
  );
}

function userVisibleIdentityError(error: unknown): string {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code?: unknown }).code ?? "")
      : "";
  const kind =
    typeof error === "object" && error !== null && "kind" in error
      ? String((error as { kind?: unknown }).kind ?? "")
      : "";
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (
    code === "IDENTITY_STORE_UNAVAILABLE" ||
    kind === "secure-store-unavailable" ||
    message.includes("encrypted credential storage") ||
    message.includes("safestorage") ||
    message.includes("secure store")
  ) {
    return "安全身份存储不可用；EchoDesk 已停止身份连接";
  }
  if (kind === "invalid-origin" || message.includes("https")) {
    return "设备身份仅允许通过 HTTPS 连接；未发送任何设备凭证";
  }
  return "暂时无法验证设备身份，请检查连接或安全存储后重试";
}

function publishCompatibility(next: BackendCompatibility): void {
  compatibility = next;
  if (typeof document !== "undefined") {
    document.documentElement.dataset.backendCompatibility = next;
  }
  if (next === "legacy") {
    console.warn("[backend] 旧后端不支持 EchoDesk 0.3 bootstrap/session 能力，已降级");
  }
}

function publishCompatibilityForOrigin(
  origin: string,
  next: BackendCompatibility,
): void {
  if (activeBackendOrigin === origin) publishCompatibility(next);
}

export function backendCompatibility(): BackendCompatibility {
  return compatibility;
}

async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), BOOTSTRAP_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

async function loadBootstrap(origin: string): Promise<BackendBootstrap | null> {
  try {
    const response = await fetchWithTimeout(
      await backendUrlForOrigin("/bootstrap", origin),
      {
      cache: "no-store",
      },
    );
    if (response.status === 404) {
      publishCompatibilityForOrigin(origin, "legacy");
      return null;
    }
    if (!response.ok) throw new Error(`bootstrap ${response.status}`);
    const body = (await response.json()) as Partial<BackendBootstrap>;
    if (
      body.schema_version !== 1 ||
      body.api_version !== "0.3" ||
      typeof body.session_required !== "boolean" ||
      !body.capabilities
    ) {
      publishCompatibilityForOrigin(origin, "legacy");
      return null;
    }
    publishCompatibilityForOrigin(origin, "compatible");
    return body as BackendBootstrap;
  } catch (error) {
    publishCompatibilityForOrigin(origin, "unreachable");
    console.warn("[backend] bootstrap unavailable", error);
    return null;
  }
}

function bootstrapBackendForOrigin(
  origin: string,
): Promise<BackendBootstrap | null> {
  if (bootstrapPromise && bootstrapPromiseOrigin === origin) {
    return bootstrapPromise;
  }
  bootstrapPromiseOrigin = origin;
  bootstrapPromise = loadBootstrap(origin);
  return bootstrapPromise;
}

export async function bootstrapBackend(): Promise<BackendBootstrap | null> {
  const origin = await configuredBackendOrigin();
  selectBackendOrigin(origin);
  return bootstrapBackendForOrigin(origin);
}

function acceptSession(
  body: IssuedSessionResponse | null,
  origin: string,
): string | null {
  const token = body?.token?.trim() || null;
  if (!token) {
    if (activeBackendOrigin === origin) currentSession = null;
    return null;
  }
  const tenantId = body?.principal?.tenant_id;
  const ownerId = body?.principal?.owner_id;
  const scopeKey =
    typeof tenantId === "string" && typeof ownerId === "string"
      ? `${tenantId}:${ownerId}`
      : "public-session";
  if (activeBackendOrigin === origin) {
    currentSession = {
      token,
      expiresAt: body?.expires_at ?? null,
      scopeKey,
      origin,
    };
    publishIdentityStatus("ready");
  }
  return token;
}

function readMemoryToken(origin: string): string | null {
  if (!currentSession || currentSession.origin !== origin) return null;
  if (
    currentSession.expiresAt &&
    Date.parse(currentSession.expiresAt) <= Date.now() + 5_000
  ) {
    currentSession = null;
    return null;
  }
  return currentSession.token;
}

function identityLost(
  origin: string,
  cause?: unknown,
): IdentityCredentialStoreError {
  const error = new IdentityCredentialStoreError(
    "设备身份已失效；为保护历史数据，EchoDesk 不会自动创建新的 owner",
    "identity-lost",
    { cause },
  );
  if (activeBackendOrigin === origin) {
    currentSession = null;
    publishIdentityStatus("identity-lost", error.message);
  }
  return error;
}

function selectBackendOrigin(origin: string): void {
  if (activeBackendOrigin === origin) return;
  activeBackendOrigin = origin;
  currentSession = null;
  compatibility = "unknown";
  if (typeof document !== "undefined") {
    document.documentElement.dataset.backendCompatibility = "unknown";
  }
}

function publishIdentityStatusForOrigin(
  origin: string,
  phase: SessionIdentityPhase,
  message: string | null = null,
): void {
  if (activeBackendOrigin === origin) publishIdentityStatus(phase, message);
}

async function configuredBackendOrigin(): Promise<string> {
  const base = await backendBase();
  const endpoint = base || (await apiUrl("/bootstrap"));
  return new URL(endpoint, window.location.href).origin;
}

async function backendUrlForOrigin(
  endpoint: string,
  expectedOrigin: string,
): Promise<string> {
  const url = await apiUrl(endpoint);
  const actualOrigin = new URL(url, window.location.href).origin;
  if (actualOrigin !== expectedOrigin) {
    throw new Error("后端地址已切换，请重新发起请求");
  }
  return url;
}

async function postDeviceSession(
  endpoint: "/session/enroll" | "/session/renew",
  body: Record<string, string>,
  origin: string,
): Promise<IssuedSessionResponse> {
  const response = await fetchWithTimeout(await backendUrlForOrigin(endpoint, origin), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 404) {
    publishCompatibilityForOrigin(origin, "legacy");
    throw new DeviceSessionRequestError(endpoint, response.status);
  }
  if (!response.ok) throw new DeviceSessionRequestError(endpoint, response.status);
  return (await response.json()) as IssuedSessionResponse;
}

async function withRendererIdentityLock<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const previous = rendererIdentityTail;
  let release: () => void = () => {};
  rendererIdentityTail = new Promise<void>((resolve) => {
    release = resolve;
  });
  await previous;
  try {
    return await operation();
  } finally {
    release();
  }
}

async function markRendererIdentityLost(
  origin: string,
  cause: unknown,
): Promise<never> {
  try {
    await identityCredentialStore.markIdentityLost(origin);
  } catch {
    // The observable identity-lost state must not be hidden by a secondary vault error.
  }
  throw identityLost(origin, cause);
}

function isIdentityRejection(error: unknown): error is DeviceSessionRequestError {
  return (
    error instanceof DeviceSessionRequestError &&
    (error.status === 401 || error.status === 409)
  );
}

async function recoverPendingRotation(
  origin: string,
  pending: NonNullable<
    Awaited<ReturnType<typeof identityCredentialStore.loadOrCreate>>["pending_rotation"]
  >,
  restoreBeforeFinalize = false,
): Promise<string> {
  try {
    const renewed = await postDeviceSession("/session/renew", {
      device_credential: pending.new_device_credential,
    }, origin);
    if (restoreBeforeFinalize) {
      await identityCredentialStore.restoreIdentity(origin);
    }
    await identityCredentialStore.commitRotation(origin, pending.rotation_id);
    const token = acceptSession(renewed, origin);
    if (!token) throw new Error("rotation recovery returned no access token");
    return token;
  } catch (pendingError) {
    if (
      pendingError instanceof DeviceSessionRequestError &&
      pendingError.status === 409
    ) {
      return markRendererIdentityLost(origin, pendingError);
    }
    if (
      !(pendingError instanceof DeviceSessionRequestError) ||
      pendingError.status !== 401
    ) {
      // Unknown server outcome: retain pending so the exact same new secret is retried.
      throw pendingError;
    }
  }

  try {
    const renewed = await postDeviceSession("/session/renew", {
      device_credential: pending.current_device_credential,
    }, origin);
    if (restoreBeforeFinalize) {
      await identityCredentialStore.restoreIdentity(origin);
    }
    await identityCredentialStore.abortRotation(origin, pending.rotation_id);
    const token = acceptSession(renewed, origin);
    if (!token) throw new Error("rotation rollback returned no access token");
    return token;
  } catch (currentError) {
    if (isIdentityRejection(currentError)) {
      return markRendererIdentityLost(origin, currentError);
    }
    // The old credential may still be valid; retain pending until a definitive result.
    throw currentError;
  }
}

async function rendererDeviceSessionUnlocked(origin: string): Promise<string> {
  const identity = await identityCredentialStore.loadOrCreate(origin);
  if (identity.pending_rotation) {
    return recoverPendingRotation(origin, identity.pending_rotation);
  }
  const requiresEnrollment = !identity.enrollment_confirmed;
  try {
    const session = requiresEnrollment
      ? await postDeviceSession("/session/enroll", {
          enrollment_id: identity.enrollment_id,
          device_secret: identity.device_secret,
        }, origin)
      : await postDeviceSession("/session/renew", {
          device_credential: identity.device_secret,
        }, origin);
    if (requiresEnrollment) {
      await identityCredentialStore.confirmEnrollment(origin);
    }
    const token = acceptSession(session, origin);
    if (!token) throw new Error("identity endpoint returned no access token");
    return token;
  } catch (error) {
    if (isIdentityRejection(error)) {
      return markRendererIdentityLost(origin, error);
    }
    throw error;
  }
}

async function rendererDeviceSession(origin: string): Promise<string> {
  return withRendererIdentityLock(() => rendererDeviceSessionUnlocked(origin));
}

function electronErrorIsIdentityLost(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const code = "code" in error ? String((error as { code?: unknown }).code ?? "") : "";
  const message =
    error instanceof Error
      ? error.message.toLowerCase()
      : "message" in error
        ? String((error as { message?: unknown }).message ?? "").toLowerCase()
        : "";
  return code === "IDENTITY_LOST" || message.includes("refusing to enroll");
}

async function electronDeviceSession(force: boolean, origin: string): Promise<string> {
  const request = force
    ? window.echo?.renewPublicSession
    : window.echo?.ensurePublicSession;
  if (!request) throw new Error("Electron credential bridge is unavailable");
  try {
    const token = acceptSession(await request(), origin);
    if (!token) throw identityLost(origin);
    return token;
  } catch (error) {
    if (isIdentityLostError(error)) throw error;
    if (electronErrorIsIdentityLost(error)) throw identityLost(origin, error);
    throw error;
  }
}

async function establishDeviceSession(
  force: boolean,
  origin: string,
): Promise<string> {
  if (window.echo?.isElectron === true) return electronDeviceSession(force, origin);
  return rendererDeviceSession(origin);
}

async function rendererReconnectSession(origin: string): Promise<string> {
  return withRendererIdentityLock(async () => {
    const identity = await identityCredentialStore.loadForReconnect(origin);
    if (identity.pending_rotation) {
      return recoverPendingRotation(origin, identity.pending_rotation, true);
    }
    try {
      const renewed = await postDeviceSession(
        "/session/renew",
        { device_credential: identity.device_secret },
        origin,
      );
      await identityCredentialStore.restoreIdentity(origin);
      const token = acceptSession(renewed, origin);
      if (!token) throw new Error("identity reconnect returned no access token");
      return token;
    } catch (error) {
      if (isIdentityRejection(error)) {
        return markRendererIdentityLost(origin, error);
      }
      throw error;
    }
  });
}

/**
 * User-initiated recovery for a persistently lost identity. It only retries the exact stored
 * credential and never enrolls a replacement owner.
 */
export async function reconnectServerIdentity(): Promise<string> {
  const origin = await configuredBackendOrigin();
  selectBackendOrigin(origin);
  currentSession = null;
  publishIdentityStatus("renewing");
  try {
    return window.echo?.isElectron === true
      ? await electronDeviceSession(true, origin)
      : await rendererReconnectSession(origin);
  } catch (error) {
    if (!isIdentityLostError(error) && activeBackendOrigin === origin) {
      publishIdentityStatus(
        "identity-lost",
        "暂时无法重新连接原设备身份，请检查网络后重试",
      );
    }
    throw error;
  }
}

export interface CredentialRotationResult {
  credential_id: string | null;
  credential_expires_at: string | null;
}

export async function rotateServerCredential(): Promise<CredentialRotationResult> {
  const origin = await configuredBackendOrigin();
  selectBackendOrigin(origin);
  const token = await ensureServerSession();
  if (!token) throw new Error("credential rotation requires an active session");
  if (currentSession?.origin !== origin || currentSession.token !== token) {
    throw new Error("后端地址已切换，请重新发起凭证轮换");
  }
  if (window.echo?.isElectron === true) {
    const rotate = window.echo.rotatePublicCredential;
    if (!rotate) throw new Error("Electron credential rotation bridge is unavailable");
    return rotate(token);
  }

  return withRendererIdentityLock(async () => {
    const rotation = await identityCredentialStore.beginRotation(origin);
    // A lost response may mean the server committed; leaving pending intact enables recovery.
    const response = await fetchWithTimeout(
      await backendUrlForOrigin("/session/credential/rotate", origin),
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          current_device_credential: rotation.current_device_credential,
          new_device_credential: rotation.new_device_credential,
        }),
      },
    );

    if (!response.ok) {
      const outcome = classifyCredentialRotationStatus(response.status);
      if (outcome === "identity-lost") {
        return markRendererIdentityLost(
          origin,
          new DeviceSessionRequestError(
            "/session/credential/rotate",
            response.status,
          ),
        );
      }
      if (outcome === "definitive-rejection") {
        await identityCredentialStore.abortRotation(origin, rotation.rotation_id);
      }
      throw new DeviceSessionRequestError(
        "/session/credential/rotate",
        response.status,
      );
    }
    const body = (await response.json()) as Partial<CredentialRotationResult>;
    await identityCredentialStore.commitRotation(origin, rotation.rotation_id);
    return {
      credential_id:
        typeof body.credential_id === "string" ? body.credential_id : null,
      credential_expires_at:
        typeof body.credential_expires_at === "string"
          ? body.credential_expires_at
          : null,
    };
  });
}

export async function ensureServerSession(force = false): Promise<string | null> {
  const origin = await configuredBackendOrigin();
  selectBackendOrigin(origin);
  if (force) {
    currentSession = null;
    if (forcedRenewPromise && forcedRenewPromiseOrigin === origin) {
      return forcedRenewPromise;
    }
    const establishing =
      sessionPromiseOrigin === origin ? sessionPromise : null;
    const pending = (async () => {
      if (establishing) {
        try {
          await establishing;
        } catch {
          // A failed initial operation does not prevent the explicit renewal attempt.
        }
      }
      const bootstrap = await bootstrapBackendForOrigin(origin);
      const requiresSession =
        bootstrap?.session_required ?? shouldHideSharedPublicHistory();
      if (!requiresSession) {
        publishIdentityStatusForOrigin(origin, "idle");
        return null;
      }
      publishIdentityStatusForOrigin(origin, "renewing");
      return establishDeviceSession(true, origin);
    })();
    forcedRenewPromise = pending;
    forcedRenewPromiseOrigin = origin;
    try {
      return await pending;
    } catch (error) {
      if (isIdentityLostError(error)) {
        publishIdentityStatusForOrigin(origin, "identity-lost", error.message);
      } else if (activeBackendOrigin === origin) {
        publishIdentityStatusForOrigin(
          origin,
          "error",
          userVisibleIdentityError(error),
        );
      }
      throw error;
    } finally {
      if (forcedRenewPromise === pending) {
        forcedRenewPromise = null;
        forcedRenewPromiseOrigin = null;
      }
    }
  }
  if (forcedRenewPromise && forcedRenewPromiseOrigin === origin) {
    return forcedRenewPromise;
  }
  const active = readMemoryToken(origin);
  if (active) return active;
  if (sessionPromise && sessionPromiseOrigin === origin) return sessionPromise;
  const pending = (async () => {
    const bootstrap = await bootstrapBackendForOrigin(origin);
    const requiresSession = bootstrap?.session_required ?? shouldHideSharedPublicHistory();
    if (!requiresSession) {
      publishIdentityStatusForOrigin(origin, "idle");
      return null;
    }
    publishIdentityStatusForOrigin(origin, "establishing");
    return establishDeviceSession(false, origin);
  })();
  sessionPromise = pending;
  sessionPromiseOrigin = origin;
  try {
    return await pending;
  } catch (error) {
    if (isIdentityLostError(error)) {
      publishIdentityStatusForOrigin(origin, "identity-lost", error.message);
    } else if (activeBackendOrigin === origin) {
      publishIdentityStatusForOrigin(
        origin,
        "error",
        userVisibleIdentityError(error),
      );
    }
    throw error;
  } finally {
    if (sessionPromise === pending) {
      sessionPromise = null;
      sessionPromiseOrigin = null;
    }
  }
}

function withAuthorization(init: RequestInit, token: string | null): RequestInit {
  if (!token) return init;
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const requestOrigin = await backendRequestOrigin(input);
  if (!requestOrigin) {
    return fetch(input, init);
  }
  const token = await ensureServerSession();
  assertSessionTokenOrigin(token, requestOrigin);
  let response = await fetch(input, withAuthorization(init, token));
  if (response.status !== 401 || !token) return response;
  const refreshed = await ensureServerSession(true);
  assertSessionTokenOrigin(refreshed, requestOrigin);
  response = await fetch(input, withAuthorization(init, refreshed));
  return response;
}

function assertSessionTokenOrigin(
  token: string | null,
  expectedOrigin: string,
): void {
  if (token && currentSession?.origin !== expectedOrigin) {
    throw new Error("后端地址已切换，已停止发送旧会话凭证");
  }
}

async function backendRequestOrigin(
  input: RequestInfo | URL,
): Promise<string | null> {
  const configuredBase = await backendBase();
  const expectedOrigin = configuredBase
    ? new URL(configuredBase).origin
    : window.location.origin;
  try {
    return new URL(requestUrl(input), window.location.href).origin === expectedOrigin
      ? expectedOrigin
      : null;
  } catch {
    return null;
  }
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

async function httpErrorDetail(response: Response): Promise<string> {
  try {
    return (await response.clone().text()).slice(0, 500);
  } catch {
    return "";
  }
}

/**
 * EchoDesk backend 的唯一 renderer transport：
 * - 自动复用/续签 server-issued session；
 * - 每个请求都有超时，且与调用方 AbortSignal 正确组合；
 * - timeout / caller abort / network / HTTP error 使用同一错误类型。
 */
export async function apiTransport(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: ApiTransportOptions = {},
): Promise<Response> {
  const url = requestUrl(input);
  const timeoutMs = options.timeoutMs ?? DEFAULT_API_TIMEOUT_MS;
  const controller = new AbortController();
  const externalSignal = init.signal;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort(new DOMException("API request timed out", "TimeoutError"));
  }, Math.max(1, timeoutMs));

  try {
    const transport = options.anonymous === true ? fetch : authenticatedFetch;
    const response = await transport(input, {
      ...init,
      credentials: options.anonymous === true ? "omit" : init.credentials,
      signal: controller.signal,
    });
    if (options.throwHttpErrors !== false && !response.ok) {
      const detail = await httpErrorDetail(response);
      throw new ApiTransportError(
        `HTTP ${response.status}${detail ? `: ${detail}` : ""}`,
        "http",
        url,
        response.status,
        detail || null,
      );
    }
    return response;
  } catch (error) {
    if (error instanceof ApiTransportError) throw error;
    if (error instanceof IdentityCredentialStoreError) throw error;
    if (timedOut) {
      throw new ApiTransportError(
        `请求超时（${Math.ceil(timeoutMs / 1000)} 秒）`,
        "timeout",
        url,
        null,
        null,
        { cause: error },
      );
    }
    if (externalSignal?.aborted || isAbortError(error)) {
      throw new ApiTransportError("请求已取消", "aborted", url, null, null, {
        cause: error,
      });
    }
    throw new ApiTransportError("无法连接 EchoDesk 服务", "network", url, null, null, {
      cause: error,
    });
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

export interface AuthenticatedWsConnection {
  url: string;
  token: string | null;
  cursorKey: string;
}

export async function authenticatedWsConnection(): Promise<AuthenticatedWsConnection> {
  const origin = await configuredBackendOrigin();
  selectBackendOrigin(origin);
  const url = new URL(await backendWsUrl());
  const wsHttpOrigin = `${url.protocol === "wss:" ? "https:" : "http:"}//${url.host}`;
  if (wsHttpOrigin !== origin) {
    throw new Error("后端地址已切换，已停止建立旧身份 WebSocket");
  }
  const token = await ensureServerSession();
  assertSessionTokenOrigin(token, origin);
  url.searchParams.delete("session");
  return {
    url: url.toString(),
    token,
    cursorKey: currentSession?.scopeKey ?? "local-session",
  };
}

export async function authenticatedWsUrl(): Promise<string> {
  return (await authenticatedWsConnection()).url;
}

export function resetSessionForTest(): void {
  bootstrapPromise = null;
  bootstrapPromiseOrigin = null;
  sessionPromise = null;
  sessionPromiseOrigin = null;
  forcedRenewPromise = null;
  forcedRenewPromiseOrigin = null;
  currentSession = null;
  activeBackendOrigin = null;
  resetIdentityCredentialStoreForTest();
  rendererIdentityTail = Promise.resolve();
  compatibility = "unknown";
  publishIdentityStatus("idle");
}

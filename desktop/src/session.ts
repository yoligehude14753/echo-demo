import {
  BACKEND_ORIGIN_EVENT,
  apiUrl,
  backendBase,
  backendWsUrl,
  compareVersions,
  shouldHideSharedPublicHistory,
  usesElectronViteProxy,
  type ElectronBackendBuildContract,
} from "@/runtime";
import {
  IdentityCredentialStoreError,
  identityCredentialStore,
  resetIdentityCredentialStoreForTest,
} from "@/identityCredentialStore";

const BOOTSTRAP_TIMEOUT_MS = 4_000;
const DEFAULT_API_TIMEOUT_MS = 180_000;
const DEFAULT_API_MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
const MAX_IDENTITY_RESPONSE_BYTES = 1024 * 1024;
const MAX_HTTP_ERROR_DETAIL_BYTES = 4 * 1024;
const PUBLIC_CLIENT_VERSION_HEADER = "X-EchoDesk-Client-Version";
const PUBLIC_MINIMUM_CLIENT_VERSION_HEADER =
  "X-EchoDesk-Minimum-Client-Version";
const LOCAL_BACKEND_PRODUCT_ID = "com.echodesk.app.backend";
const LOCAL_BACKEND_API_CONTRACT = "echodesk.desktop-backend/v1";
const LOCAL_BUILD_CONTRACT_SCHEMA_VERSION = 1;
const REQUIRED_LOCAL_CAPABILITIES = Object.freeze({
  principal_sessions: true,
  owner_isolation: true,
  workflow_kernel: "dispatcher-v1",
  ws_owner_filtering: true,
  ws_stream_epoch: true,
  ws_hello_bearer: false,
  server_resync_rehydrate_required: true,
  host_runtime_requires_admin: false,
});
export const SESSION_IDENTITY_EVENT = "echodesk:session-identity";

export type SessionIdentityPhase =
  | "idle"
  | "establishing"
  | "ready"
  | "renewing"
  | "upgrade-required"
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
  app_version?: string;
  build_contract?: ElectronBackendBuildContract;
  minimum_client_version?: string;
  session_required: boolean;
  capabilities: Record<string, unknown>;
}

export type BackendReachability = "unknown" | "reachable" | "unreachable";
export type BackendAuthReadiness =
  | "unknown"
  | "pending"
  | "not_required"
  | "authenticated"
  | "failed";
export type BackendApiContractReadiness =
  | "unknown"
  | "compatible"
  | "legacy"
  | "mismatch";
export type TranscriptionReadiness =
  | "ready"
  | "degraded"
  | "unavailable"
  | "unknown";

export interface BackendReadiness {
  reachability: BackendReachability;
  auth: BackendAuthReadiness;
  api_contract: BackendApiContractReadiness;
  transcription_readiness: TranscriptionReadiness;
}

export type BackendReadinessDiagnosticCode =
  | "readiness_unknown_malformed"
  | "readiness_unknown_stale";

const INITIAL_BACKEND_READINESS: BackendReadiness = Object.freeze({
  reachability: "unknown",
  auth: "unknown",
  api_contract: "unknown",
  transcription_readiness: "unknown",
});

export type BackendCompatibility =
  | "unknown"
  | "compatible"
  | "incompatible"
  | "legacy"
  | "upgrade-required"
  | "unreachable";

export class ClientUpgradeRequiredError extends Error {
  constructor(public readonly minimumVersion: string | null) {
    super(
      minimumVersion
        ? `需要 EchoDesk ${minimumVersion} 或更高版本才能连接公共服务`
        : "当前 EchoDesk 版本过低，请升级后再连接公共服务",
    );
    this.name = "ClientUpgradeRequiredError";
  }
}

export class BackendContractMismatchError extends Error {
  constructor(public readonly reason: string) {
    super("EchoDesk 服务合同与当前客户端不匹配，已拒绝连接");
    this.name = "BackendContractMismatchError";
    this.code = "backend_contract_mismatch";
  }

  readonly code: "backend_contract_mismatch";
}

export type BackendReadinessFailureCode =
  | "backend_unreachable"
  | "backend_auth_failed";

export class BackendReadinessError extends Error {
  constructor(public readonly code: BackendReadinessFailureCode) {
    super(
      code === "backend_auth_failed"
        ? "EchoDesk 服务身份验证未通过，已停止连接"
        : "EchoDesk 服务当前不可达，已停止连接",
    );
    this.name = "BackendReadinessError";
    this.reason = code;
  }

  readonly reason: BackendReadinessFailureCode;
}

export type ApiTransportErrorKind =
  | "timeout"
  | "aborted"
  | "stale-origin"
  | "redirect-forbidden"
  | "response-too-large"
  | "replay-required"
  | "stream-branch-forbidden"
  | "network"
  | "http";

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
  /** Maximum decoded response bytes exposed to callers, including streams. */
  maxResponseBytes?: number;
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
  /** 供 Hub 等同一用户域外的受控服务请求使用；仍受 origin 校验约束。 */
  targetOrigin?: string;
  /** 已由调用方取得的服务 token；传入后不自动建立或续签桌面 session。 */
  bearerToken?: string | null;
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
  deviceId: string | null;
  origin: string;
} | null = null;
let activeBackendOrigin: string | null = null;
let compatibility: BackendCompatibility = "unknown";
let readiness: BackendReadiness = INITIAL_BACKEND_READINESS;
let readinessDiagnosticCode: BackendReadinessDiagnosticCode | null = null;
let identityStatus: SessionIdentityStatus = { phase: "idle", message: null };
let rendererIdentityTail: Promise<void> = Promise.resolve();
let clientUpgradeRequired: {
  origin: string;
  error: ClientUpgradeRequiredError;
} | null = null;
let transportOriginEpoch = 0;
const activeTransportControllers = new Set<AbortController>();

if (typeof window !== "undefined") {
  window.addEventListener(BACKEND_ORIGIN_EVENT, () => {
    transportOriginEpoch += 1;
    for (const controller of activeTransportControllers) controller.abort();
    activeTransportControllers.clear();
  });
}

interface IssuedSessionResponse {
  token?: string | null;
  expires_at?: string | null;
  backend_origin?: string | null;
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

class BackendRedirectForbiddenError extends Error {
  constructor(public readonly endpoint: string) {
    super(`backend redirect forbidden: ${endpoint}`);
    this.name = "BackendRedirectForbiddenError";
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

function publishBackendReadiness(
  next: BackendReadiness,
  diagnostic: BackendReadinessDiagnosticCode | null = null,
  origin: string | null = activeBackendOrigin,
): void {
  if (origin !== null && activeBackendOrigin !== origin) return;
  readiness = Object.freeze({ ...next });
  readinessDiagnosticCode = diagnostic;
  if (typeof document !== "undefined") {
    document.documentElement.dataset.backendReachability = next.reachability;
    document.documentElement.dataset.backendAuth = next.auth;
    document.documentElement.dataset.backendApiContract = next.api_contract;
    document.documentElement.dataset.transcriptionReadiness =
      next.transcription_readiness;
  }
}

export function backendReadiness(): BackendReadiness {
  return { ...readiness };
}

export function transcriptionReadiness(): TranscriptionReadiness {
  return readiness.transcription_readiness;
}

export function backendReadinessDiagnosticCode(): BackendReadinessDiagnosticCode | null {
  return readinessDiagnosticCode;
}

const TRANSCRIPTION_REASON_CODES = new Set([
  "capacity_degraded",
  "maintenance",
  "temporarily_unavailable",
  "unknown",
]);

interface NormalizedTranscriptionReadiness {
  status: TranscriptionReadiness;
  diagnostic: BackendReadinessDiagnosticCode | null;
}

function normalizeTranscriptionReadiness(
  capabilities: Record<string, unknown>,
): NormalizedTranscriptionReadiness {
  if (!("transcription_readiness" in capabilities)) {
    return { status: "unknown", diagnostic: null };
  }
  const raw = capabilities.transcription_readiness;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  const value = raw as Record<string, unknown>;
  if (
    value.schema_version !== 1 ||
    (value.status !== "ready" &&
      value.status !== "degraded" &&
      value.status !== "unavailable" &&
      value.status !== "unknown") ||
    typeof value.accepting !== "boolean" ||
    typeof value.checked_at !== "string" ||
    !value.checked_at.endsWith("Z") ||
    !Number.isFinite(Date.parse(value.checked_at)) ||
    typeof value.ttl_s !== "number" ||
    !Number.isFinite(value.ttl_s) ||
    value.ttl_s < 0
  ) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  const checkedAt = Date.parse(value.checked_at);
  if (Date.now() > checkedAt + value.ttl_s * 1000) {
    return { status: "unknown", diagnostic: "readiness_unknown_stale" };
  }
  if (
    (value.status === "ready" || value.status === "degraded") &&
    value.accepting !== true
  ) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  if (
    (value.status === "unavailable" || value.status === "unknown") &&
    value.accepting !== false
  ) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  if (
    "reason_code" in value &&
    value.reason_code !== undefined &&
    (typeof value.reason_code !== "string" ||
      !TRANSCRIPTION_REASON_CODES.has(value.reason_code))
  ) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  if (
    "retry_after_s" in value &&
    value.retry_after_s !== undefined &&
    (typeof value.retry_after_s !== "number" ||
      !Number.isFinite(value.retry_after_s) ||
      value.retry_after_s < 0)
  ) {
    return { status: "unknown", diagnostic: "readiness_unknown_malformed" };
  }
  return { status: value.status as TranscriptionReadiness, diagnostic: null };
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
  if (error instanceof ClientUpgradeRequiredError) return error.message;
  if (error instanceof BackendContractMismatchError) return error.message;
  if (error instanceof BackendReadinessError) return error.message;
  if (
    error instanceof IdentityCredentialStoreError &&
    error.kind === "invalid-origin"
  ) {
    return error.message;
  }
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
  } else if (next === "incompatible") {
    console.warn("[backend] 本机 backend 构建契约不匹配，已停止业务请求");
  } else if (next === "upgrade-required") {
    console.warn("[backend] 当前客户端低于服务端最低版本，已停止身份与业务请求");
  }
}

function publishCompatibilityForOrigin(
  origin: string,
  next: BackendCompatibility,
): void {
  if (activeBackendOrigin !== origin) return;
  if (
    next !== "upgrade-required" &&
    clientUpgradeRequired?.origin === origin
  ) {
    return;
  }
  publishCompatibility(next);
}

export function backendCompatibility(): BackendCompatibility {
  return compatibility;
}

function normalizedMinimumVersion(raw: string | null | undefined): string | null {
  const value = String(raw ?? "").trim();
  return /^(?:v)?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(value) &&
    value.length <= 64
    ? value.replace(/^v/, "")
    : null;
}

export function markClientUpgradeRequired(
  minimumVersion: string | null = null,
  origin: string | null = activeBackendOrigin,
): ClientUpgradeRequiredError {
  const error = new ClientUpgradeRequiredError(
    normalizedMinimumVersion(minimumVersion),
  );
  if (origin && activeBackendOrigin === origin) {
    clientUpgradeRequired = { origin, error };
    currentSession = null;
    publishCompatibility("upgrade-required");
    publishIdentityStatus("upgrade-required", error.message);
  }
  return error;
}

function upgradeErrorFromResponse(
  response: Response,
  origin: string | null = activeBackendOrigin,
): ClientUpgradeRequiredError | null {
  if (response.status !== 426) return null;
  return markClientUpgradeRequired(
    response.headers.get(PUBLIC_MINIMUM_CLIENT_VERSION_HEADER),
    origin,
  );
}

function throwIfClientUpgradeRequired(origin: string): void {
  if (clientUpgradeRequired?.origin === origin) {
    throw clientUpgradeRequired.error;
  }
}

const REDIRECT_RESPONSE_STATUSES = new Set([301, 302, 303, 307, 308]);

function isRedirectResponse(response: Response): boolean {
  return (
    response.type === "opaqueredirect" ||
    REDIRECT_RESPONSE_STATUSES.has(response.status)
  );
}

async function readBoundedResponseBody(
  response: Response,
  signal: AbortSignal,
): Promise<ArrayBuffer | null> {
  if (!response.body) return null;
  const declared = Number(response.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > MAX_IDENTITY_RESPONSE_BYTES) {
    await response.body.cancel("identity response exceeds size limit");
    throw new Error("identity response exceeds size limit");
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  const read = (): Promise<ReadableStreamReadResult<Uint8Array>> =>
    new Promise((resolve, reject) => {
      const onAbort = () => {
        void reader.cancel(signal.reason).catch(() => {});
        reject(
          signal.reason ?? new DOMException("identity request aborted", "AbortError"),
        );
      };
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener("abort", onAbort, { once: true });
      void reader.read().then(resolve, reject).finally(() => {
        signal.removeEventListener("abort", onAbort);
      });
    });

  try {
    let complete = false;
    while (!complete) {
      const { done, value } = await read();
      if (done) {
        complete = true;
        continue;
      }
      total += value.byteLength;
      if (total > MAX_IDENTITY_RESPONSE_BYTES) {
        await reader.cancel("identity response exceeds size limit");
        throw new Error("identity response exceeds size limit");
      }
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel(error).catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }

  if (total === 0) return null;
  const buffer = new ArrayBuffer(total);
  const body = new Uint8Array(buffer);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return buffer;
}

async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const externalSignal = init.signal;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = window.setTimeout(
    () => controller.abort(new DOMException("identity request timed out", "TimeoutError")),
    BOOTSTRAP_TIMEOUT_MS,
  );
  try {
    const response = await fetch(url, {
      ...withClientVersion(init),
      redirect: "error",
      signal: controller.signal,
    });
    if (isRedirectResponse(response)) {
      await response.body?.cancel("backend redirect forbidden").catch(() => {});
      throw new BackendRedirectForbiddenError(url);
    }
    if (response.url) {
      let responseOrigin = "";
      try {
        responseOrigin = new URL(response.url).origin;
      } catch {
        responseOrigin = "";
      }
      if (responseOrigin !== new URL(url, window.location.href).origin) {
        await response.body?.cancel("backend response origin changed").catch(() => {});
        throw new BackendRedirectForbiddenError(url);
      }
    }
    const body = await readBoundedResponseBody(response, controller.signal);
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}

async function parseBoundedJsonResponse<T>(
  response: Response,
  label: "bootstrap" | "identity" | "credential rotation",
): Promise<T> {
  let text: string;
  try {
    text = await response.text();
  } catch {
    throw new Error(`${label} response could not be read`);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    // SyntaxError.message may quote the invalid server body. Never preserve it
    // as message/cause because bootstrap failures are logged by the renderer.
    throw new Error(`${label} response is invalid JSON`);
  }
}

function requireLocalBuildContract(): boolean {
  return (
    typeof window !== "undefined" &&
    window.echo?.isElectron === true &&
    window.echo?.isPublicDemo !== true
  );
}

function rejectLocalBuildContract(reason: string): never {
  throw new BackendContractMismatchError(reason);
}

function validateExpectedLocalBuildContract(
  expected: ElectronBackendBuildContract | null,
): ElectronBackendBuildContract {
  if (!expected || typeof expected !== "object") {
    return rejectLocalBuildContract("expected-contract-missing");
  }
  if (
    expected.schema_version !== LOCAL_BUILD_CONTRACT_SCHEMA_VERSION ||
    expected.product_id !== LOCAL_BACKEND_PRODUCT_ID ||
    expected.product_version !== __APP_VERSION__ ||
    expected.api_contract !== LOCAL_BACKEND_API_CONTRACT ||
    !/^sha256:[0-9a-f]{64}$/.test(expected.build_id) ||
    (expected.schema_catalog_max !== null &&
      (!Number.isSafeInteger(expected.schema_catalog_max) ||
        expected.schema_catalog_max < 1))
  ) {
    return rejectLocalBuildContract("expected-contract-invalid");
  }
  return expected;
}

async function expectedLocalBuildContract(): Promise<ElectronBackendBuildContract | null> {
  if (!requireLocalBuildContract()) return null;
  const loadExpected = window.echo?.getBackendContract;
  if (!loadExpected) {
    return rejectLocalBuildContract("expected-contract-bridge-missing");
  }
  try {
    return validateExpectedLocalBuildContract(await loadExpected());
  } catch (error) {
    if (error instanceof BackendContractMismatchError) throw error;
    return rejectLocalBuildContract("expected-contract-unavailable");
  }
}

function validateLocalBuildContract(
  bootstrap: Partial<BackendBootstrap>,
  expected: ElectronBackendBuildContract,
): void {
  if (
    bootstrap.session_required !== false ||
    bootstrap.backend_version !== expected.product_version ||
    bootstrap.app_version !== expected.product_version
  ) {
    rejectLocalBuildContract("product-version-mismatch");
  }
  const actual = bootstrap.build_contract;
  if (!actual || typeof actual !== "object") {
    rejectLocalBuildContract("build-contract-missing");
  }
  for (const key of [
    "schema_version",
    "product_id",
    "product_version",
    "api_contract",
    "build_id",
  ] as const) {
    if (actual[key] !== expected[key]) {
      rejectLocalBuildContract(`${key.replaceAll("_", "-")}-mismatch`);
    }
  }
  if (
    !Number.isSafeInteger(actual.schema_catalog_max) ||
    Number(actual.schema_catalog_max) < 1 ||
    (expected.schema_catalog_max !== null &&
      actual.schema_catalog_max !== expected.schema_catalog_max)
  ) {
    rejectLocalBuildContract("schema-catalog-mismatch");
  }
  const capabilities = bootstrap.capabilities;
  if (!capabilities || typeof capabilities !== "object") {
    rejectLocalBuildContract("capabilities-missing");
  }
  for (const [name, required] of Object.entries(REQUIRED_LOCAL_CAPABILITIES)) {
    if (capabilities[name] !== required) {
      rejectLocalBuildContract(`capability-${name}-mismatch`);
    }
  }
}

async function loadBootstrap(origin: string): Promise<BackendBootstrap | null> {
  const localBuildContractRequired = requireLocalBuildContract();
  try {
    const expected = await expectedLocalBuildContract();
    const response = await fetchWithTimeout(
      await backendUrlForOrigin("/bootstrap", origin),
      {
        cache: "no-store",
      },
    );
    if (response.status === 401 || response.status === 403) {
      publishBackendReadiness(
        {
          reachability: "reachable",
          auth: "failed",
          api_contract: "unknown",
          transcription_readiness: "unknown",
        },
        null,
        origin,
      );
      throw new BackendReadinessError("backend_auth_failed");
    }
    if (response.status === 404) {
      throw new BackendContractMismatchError("bootstrap-missing");
    }
    if (!response.ok) {
      publishBackendReadiness(
        {
          reachability: "reachable",
          auth: "unknown",
          api_contract: "unknown",
          transcription_readiness: "unknown",
        },
        null,
        origin,
      );
      throw new BackendReadinessError("backend_unreachable");
    }
    let body: Partial<BackendBootstrap>;
    try {
      body = await parseBoundedJsonResponse<Partial<BackendBootstrap>>(
        response,
        "bootstrap",
      );
    } catch {
      throw new BackendContractMismatchError("bootstrap-malformed");
    }
    if (
      body.schema_version !== 1 ||
      body.api_version !== "0.3" ||
      typeof body.session_required !== "boolean" ||
      !body.capabilities ||
      typeof body.capabilities !== "object" ||
      Array.isArray(body.capabilities)
    ) {
      throw new BackendContractMismatchError("bootstrap-contract-mismatch");
    }
    if (expected) validateLocalBuildContract(body, expected);
    const capabilities = body.capabilities as Record<string, unknown>;
    const transcription = normalizeTranscriptionReadiness(capabilities);
    publishBackendReadiness(
      {
        reachability: "reachable",
        auth: body.session_required ? "pending" : "not_required",
        api_contract: "transcription_readiness" in capabilities ? "compatible" : "legacy",
        transcription_readiness: transcription.status,
      },
      transcription.diagnostic,
      origin,
    );
    const minimumVersion = body.minimum_client_version?.trim();
    if (
      minimumVersion &&
      compareVersions(__APP_VERSION__, minimumVersion) < 0
    ) {
      throw markClientUpgradeRequired(minimumVersion, origin);
    }
    throwIfClientUpgradeRequired(origin);
    publishCompatibilityForOrigin(origin, "compatible");
    return body as BackendBootstrap;
  } catch (error) {
    if (error instanceof ClientUpgradeRequiredError) throw error;
    if (error instanceof BackendReadinessError) {
      if (error.code === "backend_unreachable") {
        publishCompatibilityForOrigin(origin, "unreachable");
      }
      throw error;
    }
    if (error instanceof BackendContractMismatchError) {
      publishBackendReadiness(
        {
          reachability:
            readiness.reachability === "unknown" ? "reachable" : readiness.reachability,
          auth: readiness.auth,
          api_contract: "mismatch",
          transcription_readiness: "unknown",
        },
        null,
        origin,
      );
      publishCompatibilityForOrigin(origin, "incompatible");
      throw error;
    }
    if (localBuildContractRequired) {
      const mismatch =
        error instanceof BackendContractMismatchError
          ? error
          : new BackendContractMismatchError("bootstrap-unavailable");
      publishBackendReadiness(
        {
          reachability: "unreachable",
          auth: "unknown",
          api_contract: "mismatch",
          transcription_readiness: "unknown",
        },
        null,
        origin,
      );
      publishCompatibilityForOrigin(origin, "incompatible");
      throw mismatch;
    }
    publishBackendReadiness(
      {
        reachability: "unreachable",
        auth: "unknown",
        api_contract: "unknown",
        transcription_readiness: "unknown",
      },
      null,
      origin,
    );
    publishCompatibilityForOrigin(origin, "unreachable");
    console.warn("[backend] bootstrap unavailable", error instanceof Error ? error.name : "unknown");
    throw new BackendReadinessError("backend_unreachable");
  }
}

function bootstrapBackendForOrigin(
  origin: string,
): Promise<BackendBootstrap | null> {
  if (bootstrapPromise && bootstrapPromiseOrigin === origin) {
    return bootstrapPromise;
  }
  bootstrapPromiseOrigin = origin;
  const pending = loadBootstrap(origin);
  bootstrapPromise = pending;
  void pending.catch((error: unknown) => {
    // The packaged renderer commonly starts before its bundled backend has
    // finished migrations and opened the socket.  Do not pin that transient
    // bootstrap failure for the lifetime of the renderer: the WebSocket retry
    // loop must be able to probe the now-ready backend on its next attempt.
    //
    // A concrete contract mismatch stays cached and therefore fail-closed;
    // replacing an incompatible backend requires an explicit origin/session
    // reset instead of silently reconnecting to a different binary.
    if (
      ((error instanceof BackendReadinessError &&
        error.code === "backend_unreachable") ||
        (error instanceof BackendContractMismatchError &&
          error.reason === "bootstrap-unavailable")) &&
      bootstrapPromise === pending &&
      bootstrapPromiseOrigin === origin
    ) {
      bootstrapPromise = null;
      bootstrapPromiseOrigin = null;
    }
  });
  return pending;
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
  throwIfClientUpgradeRequired(origin);
  const token = body?.token?.trim() || null;
  if (!token) {
    if (activeBackendOrigin === origin) currentSession = null;
    return null;
  }
  if (window.echo?.isElectron === true) {
    let sessionOrigin: string | null = null;
    try {
      const candidate = new URL(String(body?.backend_origin ?? ""));
      if (
        candidate.protocol === "https:" &&
        !candidate.username &&
        !candidate.password &&
        candidate.pathname === "/" &&
        !candidate.search &&
        !candidate.hash
      ) {
        sessionOrigin = candidate.origin;
      }
    } catch {
      sessionOrigin = null;
    }
    if (sessionOrigin !== origin) {
      throw new IdentityCredentialStoreError(
        "设备会话所属服务与当前后端不一致；已拒绝接收跨服务会话凭证",
        "invalid-origin",
      );
    }
  }
  const tenantId = body?.principal?.tenant_id;
  const ownerId = body?.principal?.owner_id;
  const principalDeviceId =
    typeof body?.principal?.device_id === "string" &&
    body.principal.device_id.trim().length > 0
      ? body.principal.device_id.trim()
      : null;
  const scopeKey =
    typeof tenantId === "string" && typeof ownerId === "string"
      ? `${tenantId}:${ownerId}`
      : "public-session";
  if (activeBackendOrigin === origin) {
    currentSession = {
      token,
      expiresAt: body?.expires_at ?? null,
      scopeKey,
      deviceId: principalDeviceId,
      origin,
    };
    publishBackendReadiness(
      { ...readiness, auth: "authenticated" },
      readinessDiagnosticCode,
      origin,
    );
    publishIdentityStatus("ready");
  }
  return token;
}

/**
 * The public backend assigns the capture-authoritative device id during
 * enrollment. It is intentionally distinct from the optional Hub sync id.
 */
export function currentSessionDeviceId(): string | null {
  return currentSession?.deviceId ?? null;
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
  clientUpgradeRequired = null;
  compatibility = "unknown";
  readiness = INITIAL_BACKEND_READINESS;
  readinessDiagnosticCode = null;
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
  // Vite proxy is only a transport origin. Identity/session scope remains
  // bound to Electron's authoritative HTTPS backend origin.
  const endpoint =
    base ||
    (usesElectronViteProxy()
      ? window.echo?.backendHost || (await apiUrl("/bootstrap"))
      : await apiUrl("/bootstrap"));
  return new URL(endpoint, window.location.href).origin;
}

async function backendUrlForOrigin(
  endpoint: string,
  expectedOrigin: string,
): Promise<string> {
  const url = await apiUrl(endpoint);
  const actualOrigin = new URL(url, window.location.href).origin;
  if (
    actualOrigin !== expectedOrigin &&
    !(usesElectronViteProxy() && actualOrigin === window.location.origin)
  ) {
    throw new Error("后端地址已切换，请重新发起请求");
  }
  return url;
}

async function postDeviceSession(
  endpoint: "/session/enroll" | "/session/renew",
  body: Record<string, string>,
  origin: string,
): Promise<IssuedSessionResponse> {
  const response = await fetchWithTimeout(
    await backendUrlForOrigin(endpoint, origin),
    withClientVersion({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
  const upgradeError = upgradeErrorFromResponse(response, origin);
  if (upgradeError) throw upgradeError;
  if (response.status === 401 || response.status === 403) {
    publishBackendReadiness(
      {
        ...readiness,
        reachability: "reachable",
        auth: "failed",
      },
      readinessDiagnosticCode,
      origin,
    );
  }
  if (response.status === 404) {
    publishCompatibilityForOrigin(origin, "legacy");
    throw new DeviceSessionRequestError(endpoint, response.status);
  }
  if (!response.ok) throw new DeviceSessionRequestError(endpoint, response.status);
  return parseBoundedJsonResponse<IssuedSessionResponse>(response, "identity");
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

function electronUpgradeRequiredVersion(error: unknown): string | null | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const code =
    "code" in error ? String((error as { code?: unknown }).code ?? "") : "";
  const message =
    error instanceof Error
      ? error.message
      : "message" in error
        ? String((error as { message?: unknown }).message ?? "")
        : "";
  if (code !== "CLIENT_UPGRADE_REQUIRED" && !message.includes("CLIENT_UPGRADE_REQUIRED")) {
    return undefined;
  }
  const explicit =
    "minimumVersion" in error
      ? String((error as { minimumVersion?: unknown }).minimumVersion ?? "")
      : message.match(/minimum=([^\s;]+)/)?.[1];
  return normalizedMinimumVersion(explicit) ?? null;
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
    const minimumVersion = electronUpgradeRequiredVersion(error);
    if (minimumVersion !== undefined) {
      throw markClientUpgradeRequired(minimumVersion, origin);
    }
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
  throwIfClientUpgradeRequired(origin);
  currentSession = null;
  publishIdentityStatus("renewing");
  try {
    return window.echo?.isElectron === true
      ? await electronDeviceSession(true, origin)
      : await rendererReconnectSession(origin);
  } catch (error) {
    if (
      !(error instanceof ClientUpgradeRequiredError) &&
      !isIdentityLostError(error) &&
      activeBackendOrigin === origin
    ) {
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
    try {
      return await rotate(token);
    } catch (error) {
      const minimumVersion = electronUpgradeRequiredVersion(error);
      if (minimumVersion !== undefined) {
        throw markClientUpgradeRequired(minimumVersion, origin);
      }
      if (electronErrorIsIdentityLost(error)) throw identityLost(origin, error);
      throw error;
    }
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

    const upgradeError = upgradeErrorFromResponse(response, origin);
    if (upgradeError) throw upgradeError;

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
    const body = await parseBoundedJsonResponse<Partial<CredentialRotationResult>>(
      response,
      "credential rotation",
    );
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
  throwIfClientUpgradeRequired(origin);
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
        } catch (error) {
          if (error instanceof ClientUpgradeRequiredError) throw error;
          // A failed initial operation does not prevent the explicit renewal attempt.
        }
      }
      throwIfClientUpgradeRequired(origin);
      const bootstrap = await bootstrapBackendForOrigin(origin);
      throwIfClientUpgradeRequired(origin);
      if (!bootstrap) throw new BackendReadinessError("backend_unreachable");
      const requiresSession =
        bootstrap.session_required || shouldHideSharedPublicHistory();
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
      if (error instanceof ClientUpgradeRequiredError) {
        // markClientUpgradeRequired already published the terminal compatibility state.
      } else if (isIdentityLostError(error)) {
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
    throwIfClientUpgradeRequired(origin);
    if (!bootstrap) throw new BackendReadinessError("backend_unreachable");
    const requiresSession =
      bootstrap.session_required || shouldHideSharedPublicHistory();
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
    if (error instanceof ClientUpgradeRequiredError) {
      // markClientUpgradeRequired already published the terminal compatibility state.
    } else if (isIdentityLostError(error)) {
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

function mergedRequestHeaders(
  input: RequestInfo | URL,
  init: RequestInit,
): Headers {
  const headers = new Headers(input instanceof Request ? input.headers : undefined);
  new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  return headers;
}

function withAuthorization(
  input: RequestInfo | URL,
  init: RequestInit,
  token: string | null,
): RequestInit {
  const headers = mergedRequestHeaders(input, init);
  headers.set(PUBLIC_CLIENT_VERSION_HEADER, __APP_VERSION__);
  headers.delete("Authorization");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

function withoutAuthorization(
  input: RequestInfo | URL,
  init: RequestInit,
): RequestInit {
  const headers = mergedRequestHeaders(input, init);
  headers.set(PUBLIC_CLIENT_VERSION_HEADER, __APP_VERSION__);
  headers.delete("Authorization");
  return { ...init, headers };
}

function withoutBackendAuthorization(
  input: RequestInfo | URL,
  init: RequestInit,
): RequestInit {
  const headers = mergedRequestHeaders(input, init);
  headers.delete("Authorization");
  return { ...init, headers, redirect: "error" };
}

function withClientVersion(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  headers.set(PUBLIC_CLIENT_VERSION_HEADER, __APP_VERSION__);
  return { ...init, headers };
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
  expectedBackendOrigin?: string,
): Promise<Response> {
  const requestOrigin =
    expectedBackendOrigin ?? (await backendRequestOrigin(input));
  if (!requestOrigin) {
    return fetch(input, withoutBackendAuthorization(input, init));
  }
  const actualOrigin = new URL(requestUrl(input), window.location.href).origin;
  if (
    actualOrigin !== requestOrigin &&
    !(usesElectronViteProxy() && actualOrigin === window.location.origin)
  ) {
    throw new Error("后端地址已切换，已拒绝访问旧服务地址");
  }
  const token = await ensureServerSession();
  assertSessionTokenOrigin(token, requestOrigin);
  let response = await fetch(
    input,
    withAuthorization(input, { ...init, redirect: "error" }, token),
  );
  const upgradeError = upgradeErrorFromResponse(response, requestOrigin);
  if (upgradeError) {
    await response.body?.cancel().catch(() => {});
    throw upgradeError;
  }
  if (response.status !== 401 || !token) return response;
  await response.body?.cancel().catch(() => {});
  const refreshed = await ensureServerSession(true);
  assertSessionTokenOrigin(refreshed, requestOrigin);
  const oneShotBody =
    (input instanceof Request && input.body !== null) ||
    init.body instanceof ReadableStream;
  if (oneShotBody) {
    throw new ApiTransportError(
      "会话凭证已更新；一次性请求体未自动重放，请重试操作",
      "replay-required",
      requestUrl(input),
      401,
    );
  }
  response = await fetch(
    input,
    withAuthorization(input, { ...init, redirect: "error" }, refreshed),
  );
  const renewedUpgradeError = upgradeErrorFromResponse(response, requestOrigin);
  if (renewedUpgradeError) {
    await response.body?.cancel().catch(() => {});
    throw renewedUpgradeError;
  }
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
    : usesElectronViteProxy()
      ? new URL(String(window.echo?.backendHost || "")).origin
      : window.location.origin;
  try {
    const actualOrigin = new URL(requestUrl(input), window.location.href).origin;
    return actualOrigin === expectedOrigin ||
      (usesElectronViteProxy() && actualOrigin === window.location.origin)
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
  // Server-controlled error bodies can contain provider diagnostics, local
  // paths, or secrets. They are never surfaced through renderer errors.
  await response.body
    ?.cancel(`HTTP error body discarded at ${MAX_HTTP_ERROR_DETAIL_BYTES} byte policy`)
    .catch(() => {});
  return "";
}

function responseWithTransportLease(
  response: Response,
  signal: AbortSignal,
  abortError: () => ApiTransportError,
  branchError: () => ApiTransportError,
  responseTooLargeError: () => ApiTransportError,
  maxResponseBytes: number,
  release: () => void,
): Response {
  if (response.status === 204 || response.status === 205 || response.status === 304) {
    void response.body?.cancel("response status forbids a body").catch(() => {});
    release();
    return new Response(null, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  }
  if (!response.body) {
    release();
    return response;
  }

  const reader = response.body.getReader();
  let settled = false;
  let receivedBytes = 0;
  let terminalError: ApiTransportError | null = null;
  let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;

  const finish = () => {
    signal.removeEventListener("abort", onAbort);
    try {
      reader.releaseLock();
    } catch {
      // cancel/read may still own the lock; release() is independently idempotent.
    }
    release();
  };
  const onAbort = () => {
    if (settled) return;
    settled = true;
    const error = abortError();
    try {
      streamController?.error(error);
    } catch {
      // A concurrently completed consumer already owns the terminal state.
    }
    void reader.cancel(error).catch(() => {}).finally(finish);
  };

  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    },
    async pull(controller) {
      if (settled) return;
      try {
        const chunk = await reader.read();
        if (settled) return;
        if (chunk.done) {
          settled = true;
          controller.close();
          finish();
          return;
        }
        receivedBytes += chunk.value.byteLength;
        if (receivedBytes > maxResponseBytes) {
          settled = true;
          const error = responseTooLargeError();
          terminalError = error;
          controller.error(error);
          void reader.cancel(error).catch(() => {}).finally(finish);
          return;
        }
        controller.enqueue(chunk.value);
      } catch (error) {
        if (settled) return;
        settled = true;
        controller.error(signal.aborted ? abortError() : error);
        finish();
      }
    },
    async cancel(reason) {
      if (settled) return;
      settled = true;
      try {
        await reader.cancel(reason);
      } finally {
        finish();
      }
    },
  });

  const leased = new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
  Object.defineProperty(leased, "clone", {
    value: () => {
      throw branchError();
    },
  });
  if (leased.body) {
    Object.defineProperty(leased.body, "tee", {
      value: () => {
        throw branchError();
      },
    });
  }
  const normalizeRead = async <T>(operation: () => Promise<T>): Promise<T> => {
    try {
      return await operation();
    } catch (error) {
      if (signal.aborted) throw abortError();
      if (terminalError) throw terminalError;
      throw error;
    }
  };
  const readArrayBuffer = leased.arrayBuffer.bind(leased);
  const readBlob = leased.blob.bind(leased);
  const readFormData = leased.formData.bind(leased);
  const readJson = leased.json.bind(leased);
  const readText = leased.text.bind(leased);
  Object.defineProperties(leased, {
    arrayBuffer: { value: () => normalizeRead(readArrayBuffer) },
    blob: { value: () => normalizeRead(readBlob) },
    formData: { value: () => normalizeRead(readFormData) },
    json: { value: () => normalizeRead(readJson) },
    text: { value: () => normalizeRead(readText) },
  });
  return leased;
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
  const maxResponseBytes =
    options.maxResponseBytes ?? DEFAULT_API_MAX_RESPONSE_BYTES;
  if (!Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1) {
    throw new TypeError("apiTransport maxResponseBytes must be a positive safe integer");
  }
  const leaseEpoch = transportOriginEpoch;
  const controller = new AbortController();
  activeTransportControllers.add(controller);
  const externalSignal =
    init.signal ?? (input instanceof Request ? input.signal : undefined);
  let timedOut = false;
  let leaseTransferred = false;
  let released = false;
  let timer = 0;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const releaseLease = () => {
    if (released) return;
    released = true;
    window.clearTimeout(timer);
    activeTransportControllers.delete(controller);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  };
  const abortTransportError = (): ApiTransportError => {
    if (leaseEpoch !== transportOriginEpoch) {
      return new ApiTransportError(
        "后端地址已切换，已取消旧服务请求",
        "stale-origin",
        url,
      );
    }
    if (timedOut) {
      return new ApiTransportError(
        `请求超时（${Math.ceil(timeoutMs / 1000)} 秒）`,
        "timeout",
        url,
      );
    }
    return new ApiTransportError("请求已取消", "aborted", url);
  };
  timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort(new DOMException("API request timed out", "TimeoutError"));
  }, Math.max(1, timeoutMs));

  try {
    const leaseOrigin = options.targetOrigin ?? (await configuredBackendOrigin());
    const actualOrigin = new URL(url, window.location.href).origin;
    if (
      leaseEpoch !== transportOriginEpoch ||
      controller.signal.aborted ||
      actualOrigin !== leaseOrigin
    ) {
      throw new ApiTransportError(
        "后端地址已切换，已取消旧服务请求",
        "stale-origin",
        url,
      );
    }
    const baseRequestInit = {
      ...init,
      credentials:
        options.anonymous === true || options.bearerToken !== undefined
          ? "omit"
          : init.credentials,
      redirect: "error" as const,
      signal: controller.signal,
    };
    const requestInit =
      options.bearerToken !== undefined
        ? withAuthorization(input, baseRequestInit, options.bearerToken)
        : options.anonymous === true
        ? withoutAuthorization(input, baseRequestInit)
        : baseRequestInit;
    const response =
      options.bearerToken !== undefined
        ? await fetch(input, requestInit)
        : options.anonymous === true
        ? await fetch(input, requestInit)
        : await authenticatedFetch(input, requestInit, leaseOrigin);
    if (isRedirectResponse(response)) {
      await response.body?.cancel("backend redirect forbidden").catch(() => {});
      throw new ApiTransportError(
        "EchoDesk 服务拒绝跨地址重定向",
        "redirect-forbidden",
        url,
        response.status || null,
      );
    }
    if (response.url) {
      let responseOrigin = "";
      try {
        responseOrigin = new URL(response.url).origin;
      } catch {
        responseOrigin = "";
      }
      if (responseOrigin !== leaseOrigin) {
        await response.body?.cancel("backend response origin changed").catch(() => {});
        throw new ApiTransportError(
          "EchoDesk 服务响应来自非预期地址",
          "redirect-forbidden",
          url,
          response.status || null,
        );
      }
    }
    if (leaseEpoch !== transportOriginEpoch || controller.signal.aborted) {
      throw new ApiTransportError(
        "后端地址已切换，已丢弃旧服务响应",
        "stale-origin",
        url,
      );
    }
    const declaredLength = response.headers.get("Content-Length")?.trim();
    if (declaredLength && (response.ok || options.throwHttpErrors === false)) {
      const declaredBytes = /^\d+$/.test(declaredLength)
        ? Number(declaredLength)
        : Number.NaN;
      if (
        !Number.isSafeInteger(declaredBytes) ||
        declaredBytes > maxResponseBytes
      ) {
        await response.body?.cancel("backend response exceeds byte limit").catch(() => {});
        throw new ApiTransportError(
          `EchoDesk 服务响应超过 ${maxResponseBytes} 字节限制`,
          "response-too-large",
          url,
          response.status || null,
        );
      }
    }
    const responseTooLargeError = () =>
      new ApiTransportError(
        `EchoDesk 服务响应超过 ${maxResponseBytes} 字节限制`,
        "response-too-large",
        url,
        response.status || null,
      );
    const leasedResponse = responseWithTransportLease(
      response,
      controller.signal,
      abortTransportError,
      () =>
        new ApiTransportError(
          "为保持后端切换隔离，EchoDesk 响应流不允许 clone/tee 分支",
          "stream-branch-forbidden",
          url,
        ),
      responseTooLargeError,
      maxResponseBytes,
      releaseLease,
    );
    leaseTransferred = true;
    if (options.throwHttpErrors !== false && !leasedResponse.ok) {
      await httpErrorDetail(leasedResponse);
      if (controller.signal.aborted) throw abortTransportError();
      await leasedResponse.body?.cancel().catch(() => {});
      throw new ApiTransportError(
        `HTTP ${leasedResponse.status}`,
        "http",
        url,
        leasedResponse.status,
        null,
      );
    }
    return leasedResponse;
  } catch (error) {
    if (error instanceof ApiTransportError) throw error;
    if (error instanceof IdentityCredentialStoreError) throw error;
    if (error instanceof ClientUpgradeRequiredError) throw error;
    if (
      leaseEpoch !== transportOriginEpoch ||
      timedOut ||
      externalSignal?.aborted ||
      isAbortError(error)
    ) {
      const normalized = abortTransportError();
      throw new ApiTransportError(
        normalized.message,
        normalized.kind,
        url,
        null,
        null,
        { cause: error },
      );
    }
    throw new ApiTransportError("无法连接 EchoDesk 服务", "network", url, null, null, {
      cause: error,
    });
  } finally {
    if (!leaseTransferred) releaseLease();
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
  if (
    wsHttpOrigin !== origin &&
    !(usesElectronViteProxy() && wsHttpOrigin === window.location.origin)
  ) {
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
  clientUpgradeRequired = null;
  resetIdentityCredentialStoreForTest();
  rendererIdentityTail = Promise.resolve();
  compatibility = "unknown";
  readiness = INITIAL_BACKEND_READINESS;
  readinessDiagnosticCode = null;
  publishIdentityStatus("idle");
}

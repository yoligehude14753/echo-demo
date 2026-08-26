export type SyncTransportAuth = "session" | "sync";

export interface PreparedSyncRequest {
  init: RequestInit;
  bearerToken: string | null;
}

/** Keep the paired gateway isolated from ordinary product API traffic. */
export function assertPairedHubPath(path: string): void {
  if (path === "/hub/v1" || path.startsWith("/hub/v1/")) return;
  throw new Error("同步网关仅允许 /hub/v1 配对和同步请求");
}

export function prepareSyncRequest(
  init: RequestInit,
  auth: SyncTransportAuth,
  token: string | null,
): PreparedSyncRequest {
  const headers = new Headers(init.headers);
  if (auth === "sync") {
    headers.delete("Authorization");
    if (token) headers.set("X-Echo-Sync-Token", token);
    else headers.delete("X-Echo-Sync-Token");
    return { init: { ...init, headers }, bearerToken: null };
  }
  headers.delete("X-Echo-Sync-Token");
  return { init: { ...init, headers }, bearerToken: token };
}

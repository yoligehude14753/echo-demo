export type SyncTransportAuth = "session" | "sync";

export interface PreparedSyncRequest {
  init: RequestInit;
  bearerToken: string | null;
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

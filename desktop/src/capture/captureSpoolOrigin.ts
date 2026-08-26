/**
 * A packaged local backend receives a fresh ephemeral loopback port after a
 * normal app restart. A durable item from the same generation may therefore
 * carry the previous loopback origin without being a foreign remote owner.
 * Remote origins remain strict; only the local backend origin is rebound at
 * upload time.
 */
export function isCaptureSpoolOriginCompatible(
  queuedOrigin: string,
  currentOrigin: string,
): boolean {
  try {
    const queued = new URL(queuedOrigin);
    const current = new URL(currentOrigin);
    if (queued.origin === current.origin) return true;
    const loopback = new Set(["127.0.0.1", "localhost", "[::1]"]);
    return (
      queued.protocol === "http:" &&
      current.protocol === "http:" &&
      queued.hostname === current.hostname &&
      loopback.has(queued.hostname) &&
      loopback.has(current.hostname)
    );
  } catch {
    return false;
  }
}

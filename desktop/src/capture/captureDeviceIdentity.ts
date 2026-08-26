import { currentSessionDeviceId } from "@/session";
import { ensureSyncDeviceId } from "@/syncState";

/**
 * Capture authorization is bound to the authenticated session principal.
 * Hub sync keeps its own optional pairing identity and remains a fallback only
 * before a public session has been established.
 */
export function captureDeviceId(): string {
  return currentSessionDeviceId() ?? ensureSyncDeviceId();
}

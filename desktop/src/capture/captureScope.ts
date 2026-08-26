/**
 * Capture attribution shared by browser and native upload paths.
 *
 * A scope is intentionally ephemeral.  A later renderer session must never
 * render a delayed result from the microphone session it replaced.
 */
export interface CaptureScope {
  deviceId: string;
  captureSessionId: string;
  source: "desktop";
}

export interface CaptureAttribution {
  device_id?: unknown;
  capture_session_id?: unknown;
  source?: unknown;
}

export function createCaptureScope(deviceId: string): CaptureScope {
  const captureSessionId =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return { deviceId, captureSessionId, source: "desktop" };
}

/** 只有仍为当前值的麦克风生命周期才能取得 active 上传所有权。 */
export function isSameCaptureScope(
  frozen: CaptureScope,
  current: CaptureScope,
): boolean {
  return (
    frozen.deviceId === current.deviceId &&
    frozen.captureSessionId === current.captureSessionId &&
    frozen.source === current.source
  );
}

/**
 * Bind only a fully unscoped legacy response to the request that received it.
 *
 * Public backend v0.3.2 accepts unknown multipart fields but does not echo
 * this tuple. A partial, null, or foreign tuple must remain fail-closed: it
 * could be an incompatible or stale response rather than the legacy shape.
 */
export function resolveCaptureResponseAttribution(
  payload: Record<string, unknown>,
  requestScope?: CaptureScope,
): CaptureAttribution {
  const hasAnyResponseScope = [
    "device_id",
    "capture_session_id",
    "source",
  ].some((key) => Object.prototype.hasOwnProperty.call(payload, key));
  if (!requestScope || hasAnyResponseScope) {
    return {
      device_id: payload.device_id,
      capture_session_id: payload.capture_session_id,
      source: payload.source,
    };
  }
  return {
    device_id: requestScope.deviceId,
    capture_session_id: requestScope.captureSessionId,
    source: requestScope.source,
  };
}

/** User-visible output is fail-closed unless it is scoped to the active capture. */
export function isCurrentCaptureAttribution(
  payload: CaptureAttribution,
  scope: CaptureScope,
): boolean {
  return (
    payload.device_id === scope.deviceId &&
    payload.capture_session_id === scope.captureSessionId &&
    payload.source === scope.source
  );
}

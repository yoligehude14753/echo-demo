import { captureDeviceId } from "@/capture/captureDeviceIdentity";

export type CaptureMode = "single" | "multi";

export interface CaptureDevice {
  deviceId: string;
  deviceName: string;
  platform: string;
  online: boolean;
}

export interface CaptureControl {
  mode: CaptureMode;
  selectedDeviceIds: string[];
  revision: number;
}

export interface CaptureControlSnapshot {
  control: CaptureControl;
  devices: CaptureDevice[];
}

export const CAPTURE_CONTROL_EVENT = "echodesk:capture-control-change";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

export function normalizeCaptureControl(value: unknown): CaptureControl {
  const body = record(value);
  const selectedDeviceIds = Array.isArray(body.selectedDeviceIds)
    ? Array.from(
        new Set(
          body.selectedDeviceIds.filter(
            (item): item is string => typeof item === "string" && item.length > 0,
          ),
        ),
      )
    : [];
  return {
    mode: body.mode === "multi" ? "multi" : "single",
    selectedDeviceIds,
    revision:
      typeof body.revision === "number" &&
      Number.isSafeInteger(body.revision) &&
      body.revision >= 0
        ? body.revision
        : 0,
  };
}

export function normalizeCaptureDevices(value: unknown): CaptureDevice[] {
  const body = record(value);
  const devices = Array.isArray(value)
    ? value
    : Array.isArray(body.devices)
      ? body.devices
      : [];
  return devices.flatMap((item) => {
    const device = record(item);
    const deviceId =
      typeof device.deviceId === "string"
        ? device.deviceId
        : typeof device.device_id === "string"
          ? device.device_id
          : "";
    if (!deviceId) return [];
    return [{
      deviceId,
      deviceName:
        typeof device.displayName === "string"
          ? device.displayName
          : typeof device.deviceName === "string"
          ? device.deviceName
          : typeof device.device_name === "string"
            ? device.device_name
            : deviceId,
      platform: typeof device.platform === "string" ? device.platform : "unknown",
      online: device.online !== false,
    }];
  });
}

export function isDeviceSelected(
  control: CaptureControl,
  deviceId = captureDeviceId(),
): boolean {
  if (control.mode === "single") {
    return control.selectedDeviceIds[0] === deviceId;
  }
  return control.selectedDeviceIds.includes(deviceId);
}

export function announceCaptureControl(control: CaptureControl): void {
  window.dispatchEvent(
    new CustomEvent<CaptureControl>(CAPTURE_CONTROL_EVENT, { detail: control }),
  );
}

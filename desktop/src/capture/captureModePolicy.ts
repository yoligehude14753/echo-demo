export type CaptureMode = "single" | "multi";

export interface CaptureDevice {
  deviceId: string;
  displayName: string;
  platform: string;
  online: boolean;
  lastSeenAt: string | null;
}

export interface CaptureControl {
  mode: CaptureMode;
  selectedDeviceIds: string[];
  revision: number;
}

export interface CaptureControlUpdate {
  mode: CaptureMode;
  selectedDeviceIds: string[];
  expectedRevision: number;
}

export function onlineCaptureDevices(devices: CaptureDevice[]): CaptureDevice[] {
  return devices.filter((device) => device.online);
}

export function buildCaptureControlUpdate(
  mode: CaptureMode,
  selectedDeviceIds: string[],
  expectedRevision: number,
): CaptureControlUpdate {
  const uniqueIds = [...new Set(selectedDeviceIds.map((id) => id.trim()).filter(Boolean))];
  if (!Number.isInteger(expectedRevision) || expectedRevision < 0) {
    throw new Error("capture revision 必须是非负整数");
  }
  if (mode === "single" && uniqueIds.length !== 1) {
    throw new Error("单端收音必须且只能选择一台设备");
  }
  if (mode === "multi" && uniqueIds.length < 1) {
    throw new Error("多端收音至少选择一台设备");
  }
  return { mode, selectedDeviceIds: uniqueIds, expectedRevision };
}

export function isDeviceSelected(
  control: CaptureControl,
  deviceId: string,
): boolean {
  return control.selectedDeviceIds.includes(deviceId);
}

import type { CaptureDevice } from "@/capture/captureControl";

export type FreeCaptureSelectionPlan =
  | { kind: "auto_single"; deviceId: string }
  | { kind: "choose"; devices: CaptureDevice[] }
  | { kind: "local_unavailable" };

/**
 * Free capture may claim the local microphone only when it is the sole online
 * device. More than one device always requires an explicit owner decision.
 */
export function planFreeCaptureSelection(
  devices: CaptureDevice[],
  localDeviceId: string,
): FreeCaptureSelectionPlan {
  const online = devices.filter((device) => device.online);
  if (online.length === 1 && online[0].deviceId === localDeviceId) {
    return { kind: "auto_single", deviceId: localDeviceId };
  }
  if (online.length > 1) return { kind: "choose", devices: online };
  return { kind: "local_unavailable" };
}

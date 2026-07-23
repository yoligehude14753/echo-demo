import type { CaptureDevice } from "@/capture/captureControl";

export type FreeCaptureSelectionPlan =
  | { kind: "auto_single"; deviceId: string }
  | { kind: "choose"; devices: CaptureDevice[] }
  | { kind: "identity_mismatch" };

export interface FreeCaptureSelfIdentity {
  /** 已认证公共会话 principal 断言的设备 ID。 */
  sessionDeviceId: string | null;
  /** 渲染器会用于标记本地采集的设备 ID。 */
  localDeviceId: string;
}

function selfCaptureCandidate(
  devices: CaptureDevice[],
  identity: FreeCaptureSelfIdentity,
): CaptureDevice | null {
  const sessionDeviceId = identity.sessionDeviceId?.trim() || null;
  if (!sessionDeviceId || sessionDeviceId !== identity.localDeviceId) return null;

  const reported = devices.find((device) => device.deviceId === sessionDeviceId);
  // Hub 清册只是远端配对视图，不能证明或否定这个已认证渲染器的本地麦克风。
  // 本机身份只使用服务端签发的 principal ID；真实权限和设备错误仍交给正常
  // getUserMedia 路径报告。
  return {
    deviceId: sessionDeviceId,
    deviceName: reported?.deviceName ?? "本机设备",
    platform: reported?.platform ?? "current-session",
    online: true,
  };
}

/**
 * 已认证的 session principal 即使在可选 Hub 清册为空（例如尚未配对）时，也能
 * 作为安全的本机收音候选。远端候选仍会合并；候选多于一个时必须由用户显式决定 owner。
 */
export function planFreeCaptureSelection(
  devices: CaptureDevice[],
  identity: FreeCaptureSelfIdentity,
): FreeCaptureSelectionPlan {
  const self = selfCaptureCandidate(devices, identity);
  if (!self) return { kind: "identity_mismatch" };

  const candidates = [
    self,
    ...devices.filter(
      (device) => device.online && device.deviceId !== self.deviceId,
    ),
  ];
  if (candidates.length === 1) {
    return { kind: "auto_single", deviceId: self.deviceId };
  }
  return { kind: "choose", devices: candidates };
}

import { Button, Checkbox, Modal, Radio, message } from "antd";
import { useEffect, useState } from "react";
import { isNativeMobile } from "@/runtime";
import { ensureSyncDeviceId } from "@/syncState";
import {
  buildCaptureControlUpdate,
  onlineCaptureDevices,
  type CaptureControl,
  type CaptureDevice,
  type CaptureMode,
} from "@/capture/captureModePolicy";
import {
  authorizeCaptureDevice,
  getCaptureControl,
  getCaptureDevices,
  putCaptureControl,
} from "@/capture/mobileCaptureApi";

const CAPTURE_AUTH_EVENT = "echodesk:android-capture-authorized";
type PendingRequest = { resolve: (allowed: boolean) => void };
let pendingRequest: PendingRequest | null = null;

export function isAndroidCaptureAuthorized(): boolean {
  if (!isNativeMobile()) return true;
  return document.documentElement.dataset.androidCaptureAuthorized === "1";
}

export function requestAndroidCaptureStart(): Promise<boolean> {
  if (!isNativeMobile()) return Promise.resolve(true);
  if (pendingRequest) return Promise.resolve(false);
  return new Promise((resolve) => {
    pendingRequest = { resolve };
    window.dispatchEvent(new Event("echodesk:android-capture-request"));
  });
}

function setAuthorized(allowed: boolean): void {
  document.documentElement.dataset.androidCaptureAuthorized = allowed ? "1" : "0";
  window.dispatchEvent(new Event(CAPTURE_AUTH_EVENT));
}

export function onAndroidCaptureAuthorizationChange(listener: () => void): () => void {
  window.addEventListener(CAPTURE_AUTH_EVENT, listener);
  return () => window.removeEventListener(CAPTURE_AUTH_EVENT, listener);
}

export default function AndroidCaptureSelector(): JSX.Element | null {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [devices, setDevices] = useState<CaptureDevice[]>([]);
  const [control, setControl] = useState<CaptureControl | null>(null);
  const [mode, setMode] = useState<CaptureMode>("single");
  const [selected, setSelected] = useState<string[]>([]);
  const localDeviceId = ensureSyncDeviceId();

  useEffect(() => {
    if (!isNativeMobile()) return;
    setAuthorized(false);
    const request = () => {
      setOpen(true);
      setLoading(true);
      void Promise.all([getCaptureDevices(), getCaptureControl()])
        .then(([nextDevices, nextControl]) => {
          const online = onlineCaptureDevices(nextDevices);
          setDevices(online);
          setControl(nextControl);
          setMode(online.length > 1 ? nextControl.mode : "single");
          setSelected(
            online.length > 1
              ? nextControl.selectedDeviceIds
              : [localDeviceId],
          );
        })
        .catch(() => {
          message.error("无法读取在线设备，请检查服务连接");
          pendingRequest?.resolve(false);
          pendingRequest = null;
          setOpen(false);
        })
        .finally(() => setLoading(false));
    };
    window.addEventListener("echodesk:android-capture-request", request);
    return () => window.removeEventListener("echodesk:android-capture-request", request);
  }, [localDeviceId]);

  if (!isNativeMobile()) return null;

  const finish = (allowed: boolean) => {
    setAuthorized(allowed);
    pendingRequest?.resolve(allowed);
    pendingRequest = null;
    setOpen(false);
  };

  const confirm = async () => {
    if (!control) return;
    setLoading(true);
    try {
      const update = buildCaptureControlUpdate(mode, selected, control.revision);
      const saved = await putCaptureControl(update);
      const auth = await authorizeCaptureDevice(localDeviceId, saved.revision);
      if (!auth.allowed) {
        message.info("这台设备保持待机，不会启用麦克风");
      }
      finish(auth.allowed);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存收音选择失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open={open}
      title="选择收音设备"
      closable={false}
      maskClosable={false}
      footer={[
        <Button key="cancel" onClick={() => finish(false)}>取消</Button>,
        <Button key="confirm" type="primary" loading={loading} onClick={confirm}>
          确认并开始
        </Button>,
      ]}
      data-testid="android-capture-selector"
    >
      {devices.length > 1 ? (
        <div className="space-y-4">
          <Radio.Group
            value={mode}
            onChange={(event) => {
              const next = event.target.value as CaptureMode;
              setMode(next);
              if (next === "single") setSelected([localDeviceId]);
            }}
          >
            <Radio value="single">单端收音</Radio>
            <Radio value="multi">多端收音</Radio>
          </Radio.Group>
          <Checkbox.Group
            className="flex flex-col gap-2"
            value={selected}
            onChange={(values) => setSelected(values.map(String))}
          >
            {devices.map((device) => (
              <Checkbox
                key={device.deviceId}
                value={device.deviceId}
                disabled={mode === "single" && device.deviceId !== localDeviceId}
              >
                {device.displayName || device.deviceId}
              </Checkbox>
            ))}
          </Checkbox.Group>
        </div>
      ) : (
        <div>将使用这台 Android 设备收音。确认前不会请求麦克风权限。</div>
      )}
    </Modal>
  );
}

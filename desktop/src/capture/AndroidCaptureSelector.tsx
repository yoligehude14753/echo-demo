/* eslint-disable react-refresh/only-export-components -- selection helpers are shared with capture contract tests */
import { Button, Checkbox, Modal, Radio, message } from "antd";
import { useCallback, useEffect, useState } from "react";
import { isNativeMobile } from "@/runtime";
import { captureDeviceId } from "@/capture/captureDeviceIdentity";
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
import { CaptureControlConflictError } from "@/capture/captureControlConflict";
import {
  isFreeCaptureEnabled,
  isFreeCapturePreferenceConfigured,
  onFreeCaptureSetupRequest,
  setFreeCaptureEnabled,
} from "@/capture/freeCaptureMode";

const CAPTURE_AUTH_EVENT = "echodesk:android-capture-authorized";
type PendingRequest = { resolve: (allowed: boolean) => void };
let pendingRequest: PendingRequest | null = null;

export function isAndroidCaptureAuthorized(): boolean {
  if (!isNativeMobile()) return true;
  return (
    isFreeCapturePreferenceConfigured() &&
    isFreeCaptureEnabled()
  );
}

export function requestAndroidCaptureStart(): Promise<boolean> {
  if (!isNativeMobile()) return Promise.resolve(true);
  if (isAndroidCaptureAuthorized()) return Promise.resolve(true);
  if (pendingRequest) return Promise.resolve(false);
  return new Promise((resolve) => {
    pendingRequest = { resolve };
    window.dispatchEvent(new Event("echodesk:android-capture-request"));
  });
}

function setAuthorized(allowed: boolean): void {
  setFreeCaptureEnabled(allowed);
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
  const localDeviceId = captureDeviceId();

  const applyAuthoritativeSnapshot = useCallback((
    nextDevices: CaptureDevice[],
    nextControl: CaptureControl,
  ) => {
    const online = onlineCaptureDevices(nextDevices);
    const authoritativeSelection = nextControl.selectedDeviceIds.filter((id) =>
      online.some((device) => device.deviceId === id),
    );
    setDevices(online);
    setControl(nextControl);
    setMode(online.length > 1 ? nextControl.mode : "single");
    setSelected(
      online.length > 1
        ? authoritativeSelection
        : [localDeviceId],
    );
  }, [localDeviceId]);

  useEffect(() => {
    if (!isNativeMobile()) return;
    const request = () => {
      setOpen(true);
      setLoading(true);
      void Promise.all([getCaptureDevices(), getCaptureControl()])
        .then(([nextDevices, nextControl]) => {
          applyAuthoritativeSnapshot(nextDevices, nextControl);
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
    const offSetupRequest = onFreeCaptureSetupRequest(() => request());
    return () => {
      window.removeEventListener("echodesk:android-capture-request", request);
      offSetupRequest();
    };
  }, [applyAuthoritativeSnapshot]);

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
        message.info("本设备未被选为收音设备");
      }
      finish(auth.allowed);
    } catch (error) {
      if (error instanceof CaptureControlConflictError) {
        try {
          const [nextDevices, nextControl] = await Promise.all([
            getCaptureDevices(),
            getCaptureControl(),
          ]);
          applyAuthoritativeSnapshot(nextDevices, nextControl);
          message.warning("收音选择已更新，请确认最新选择后重试");
        } catch {
          setControl(null);
          message.error("无法刷新最新收音选择，请取消后重新打开");
        }
      } else {
        message.error(error instanceof Error ? error.message : "保存收音选择失败");
      }
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
        <Button
          key="confirm"
          type="primary"
          loading={loading}
          disabled={!control}
          onClick={confirm}
        >
          开启自由收音
        </Button>,
      ]}
      data-testid="android-capture-selector"
      data-capture-selection="surface"
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
          <div
            className="flex flex-col gap-2"
            role="group"
            aria-label="收音设备选择"
            data-capture-selection="options"
          >
            {devices.map((device, index) => (
              <Checkbox
                key={device.deviceId}
                checked={selected.includes(device.deviceId)}
                onChange={(event) => {
                  setSelected((current) =>
                    event.target.checked
                      ? [...new Set([...current, device.deviceId])]
                      : current.filter((id) => id !== device.deviceId),
                  );
                }}
                disabled={mode === "single" && device.deviceId !== localDeviceId}
                data-capture-selection-option={
                  selected.includes(device.deviceId) ? "selected" : "available"
                }
              >
                {device.displayName && device.displayName !== device.deviceId
                  ? device.displayName
                  : `设备 ${index + 1}`}
              </Checkbox>
            ))}
          </div>
        </div>
      ) : (
        <div>将使用这台 Android 设备持续自由收音。确认后会记住选择，App 恢复时自动继续。</div>
      )}
    </Modal>
  );
}

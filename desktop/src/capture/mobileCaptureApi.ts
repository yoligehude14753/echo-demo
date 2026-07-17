import { apiUrl } from "@/runtime";
import { apiTransport } from "@/session";
import type {
  CaptureControl,
  CaptureControlUpdate,
  CaptureDevice,
  CaptureMode,
} from "@/capture/captureModePolicy";
import { CaptureControlConflictError } from "@/capture/captureControlConflict";

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`capture control HTTP ${response.status}`);
  return response.json() as Promise<T>;
}

export async function getCaptureDevices(): Promise<CaptureDevice[]> {
  const response = await apiTransport(
    await apiUrl("/capture/devices"),
    { cache: "no-store" },
    { timeoutMs: 10_000, throwHttpErrors: false },
  );
  const payload = await json<{ devices?: CaptureDevice[] }>(response);
  return Array.isArray(payload.devices) ? payload.devices : [];
}

export async function getCaptureControl(): Promise<CaptureControl> {
  const response = await apiTransport(
    await apiUrl("/capture/control"),
    { cache: "no-store" },
    { timeoutMs: 10_000, throwHttpErrors: false },
  );
  return json<CaptureControl>(response);
}

export async function putCaptureControl(
  update: CaptureControlUpdate,
): Promise<CaptureControl> {
  const response = await apiTransport(
    await apiUrl("/capture/control"),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    },
    { timeoutMs: 10_000, throwHttpErrors: false },
  );
  if (response.status === 409) {
    await response.body?.cancel().catch(() => undefined);
    throw new CaptureControlConflictError();
  }
  return json<CaptureControl>(response);
}

export async function authorizeCaptureDevice(
  deviceId: string,
  revision: number,
): Promise<{ allowed: boolean; mode: CaptureMode; revision: number }> {
  const response = await apiTransport(
    await apiUrl("/capture/control/authorize"),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deviceId, revision }),
    },
    { timeoutMs: 10_000, throwHttpErrors: false },
  );
  return json(response);
}

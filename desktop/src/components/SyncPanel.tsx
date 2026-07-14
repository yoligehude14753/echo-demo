import { Button, Input, Modal, Tag, Tooltip, message } from "antd";
import { AlertCircle, CheckCircle2, Link2, RefreshCw, Unplug } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  configuredSyncHubBase,
  setSyncHubBase,
  SYNC_HUB_BASE_EVENT,
} from "@/runtime";
import { hubStatus, type HubStatusDTO } from "@/api";
import { SyncApiError, SyncHubClient } from "@/syncApi";
import {
  clearPairing,
  loadSyncState,
  SYNC_STATE_EVENT,
  updateSyncState,
  type SyncState,
} from "@/syncState";

function statusText(state: SyncState): string {
  if (state.status === "syncing") return "同步中";
  if (state.status === "failed") return "同步失败";
  if (state.status === "synced") return "已同步";
  return "未配对";
}

function statusClass(state: SyncState): string {
  if (state.status === "failed") return "is-error";
  if (state.status === "synced") return "is-ready";
  if (state.status === "syncing") return "is-busy";
  return "is-idle";
}

function syncErrorMessage(error: unknown): string {
  if (error instanceof SyncApiError) return error.message;
  if (error instanceof Error) return error.message.slice(0, 160);
  return "配对失败，请检查配对码和网络后重试";
}

function isHostPaired(status: HubStatusDTO | null): boolean {
  return (
    status?.enabled === true &&
    status.paired === true &&
    status.connection === "connected"
  );
}

export default function SyncPanel(): JSX.Element {
  const [state, setState] = useState<SyncState>(() => loadSyncState());
  const [hostStatus, setHostStatus] = useState<HubStatusDTO | null>(null);
  const [open, setOpen] = useState(false);
  const [pairingCode, setPairingCode] = useState("");
  const [hubBase, setHubBase] = useState(() => configuredSyncHubBase() ?? "");
  const [busy, setBusy] = useState(false);
  const client = useMemo(() => new SyncHubClient(), []);
  const mountedRef = useRef(false);
  const statusRequestRef = useRef(0);

  const refreshHostStatus = useCallback(() => {
    const requestId = ++statusRequestRef.current;
    void hubStatus()
      .then((next) => {
        if (mountedRef.current && requestId === statusRequestRef.current) {
          setHostStatus(next);
        }
      })
      .catch(() => {
        // 保留最后一次成功状态，避免瞬时刷新失败把已配对 UI 闪回“未配对”。
      });
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const refresh = () => setState(loadSyncState());
    const refreshHubBase = () => setHubBase(configuredSyncHubBase() ?? "");
    window.addEventListener(SYNC_STATE_EVENT, refresh);
    window.addEventListener(SYNC_HUB_BASE_EVENT, refreshHubBase);
    window.addEventListener(SYNC_HUB_BASE_EVENT, refreshHostStatus);
    void refreshHostStatus();
    const statusTimer = window.setInterval(refreshHostStatus, 2_000);
    return () => {
      window.removeEventListener(SYNC_STATE_EVENT, refresh);
      window.removeEventListener(SYNC_HUB_BASE_EVENT, refreshHubBase);
      window.removeEventListener(SYNC_HUB_BASE_EVENT, refreshHostStatus);
      window.clearInterval(statusTimer);
    };
  }, [refreshHostStatus]);

  const openPanel = () => {
    setState(loadSyncState());
    setHubBase(configuredSyncHubBase() ?? "");
    refreshHostStatus();
    setOpen(true);
  };

  const hostPaired = isHostPaired(hostStatus);
  const displayState: SyncState = hostPaired
    ? { ...state, status: "synced", last_error: null }
    : state;

  const saveHubBase = () => {
    try {
      const saved = setSyncHubBase(hubBase);
      setHubBase(saved ?? "");
      message.success("同步服务地址已保存");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "同步服务地址无效");
    }
  };

  const pair = async () => {
    if (busy) return;
    setBusy(true);
    updateSyncState((current) => ({ ...current, status: "syncing", last_error: null }));
    try {
      await client.claimPairing(pairingCode);
      setState(loadSyncState());
      setPairingCode("");
      message.success("设备已配对，同步已就绪");
    } catch (error) {
      const detail = syncErrorMessage(error);
      updateSyncState((current) => ({ ...current, status: "failed", last_error: detail }));
      setState(loadSyncState());
      message.error(detail);
    } finally {
      setBusy(false);
    }
  };

  const unpair = () => {
    Modal.confirm({
      title: "解除此设备绑定？",
      content: "本机 device_id 和待同步内容会保留；重新配对后可继续发送。",
      okText: "解除绑定",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => {
        clearPairing();
        setState(loadSyncState());
        message.success("已解除设备绑定");
      },
    });
  };

  const pasteCode = async () => {
    try {
      const value = await navigator.clipboard.readText();
      setPairingCode(value.trim());
    } catch {
      message.info("无法读取剪贴板，请手动输入或粘贴配对码");
    }
  };

  return (
    <>
      <Tooltip title={`多端同步：${statusText(displayState)}`}>
        <button
          type="button"
          className={`sync-status ${statusClass(displayState)}`}
          data-testid="sync-status"
          onClick={openPanel}
          aria-label={`打开多端同步设置，当前${statusText(displayState)}`}
        >
          {displayState.status === "failed" ? (
            <AlertCircle aria-hidden="true" />
          ) : displayState.status === "synced" ? (
            <CheckCircle2 aria-hidden="true" />
          ) : displayState.status === "syncing" ? (
            <RefreshCw className="is-spinning" aria-hidden="true" />
          ) : (
            <Link2 aria-hidden="true" />
          )}
          <span>{statusText(displayState)}</span>
        </button>
      </Tooltip>

      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        title="多端同步"
        footer={null}
        destroyOnHidden
        width={480}
      >
        <div className="space-y-4 py-2 text-[12px] text-ink-600">
          <div className="flex items-center justify-between rounded-lg border border-paper-300 bg-paper-100 px-3 py-2">
            <span>当前状态</span>
            <Tag color={displayState.status === "failed" ? "error" : displayState.status === "synced" ? "success" : "default"}>
              {statusText(displayState)}
            </Tag>
          </div>

          <label className="block">
            <span className="mb-1 block text-ink-700">Hub 地址</span>
            <div className="flex gap-2">
              <Input
                value={hubBase}
                onChange={(event) => setHubBase(event.target.value)}
                placeholder="https://sync.example.com"
                data-testid="sync-hub-base"
              />
              <Button onClick={saveHubBase}>保存</Button>
            </div>
            <span className="mt-1 block text-[11px] text-ink-400">
              可通过 VITE_ECHODESK_SYNC_HUB_BASE 在构建时切换 Luna Hub。
            </span>
          </label>

          <label className="block">
            <span className="mb-1 block text-ink-700">输入或粘贴配对码</span>
            <div className="flex gap-2">
              <Input
                value={pairingCode}
                onChange={(event) => setPairingCode(event.target.value)}
                onPressEnter={() => void pair()}
                placeholder="输入 Hub 生成的配对码"
                maxLength={128}
                disabled={busy}
                data-testid="sync-pairing-code"
              />
              <Button onClick={() => void pasteCode()} disabled={busy}>
                粘贴
              </Button>
            </div>
          </label>

          {state.last_error && (
            <div className="rounded-lg border border-err/20 bg-err/5 px-3 py-2 text-err" role="alert">
              {state.last_error}
            </div>
          )}

          <div className="flex items-center justify-between border-t border-paper-300 pt-3 text-[11px] text-ink-400">
            <span className="font-mono">device_id: {state.device_id}</span>
            <span>待同步 {state.outbox.length} 项</span>
          </div>

          <div className="flex justify-end gap-2">
            {state.sync_token && (
              <>
                <Button
                  icon={<RefreshCw aria-hidden="true" />}
                  onClick={() => {
                    updateSyncState((current) => ({ ...current, status: "syncing", last_error: null }));
                    setState(loadSyncState());
                  }}
                  disabled={busy}
                >
                  立即重试
                </Button>
                <Button danger icon={<Unplug aria-hidden="true" />} onClick={unpair} disabled={busy}>
                  解除绑定
                </Button>
              </>
            )}
            <Button
              type="primary"
              onClick={() => void pair()}
              loading={busy}
              disabled={!pairingCode.trim()}
              data-testid="sync-pair"
            >
              配对设备
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
}

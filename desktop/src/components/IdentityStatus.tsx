import { Tooltip } from "antd";
import { Clock3, ExternalLink, RefreshCw, ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import {
  IDENTITY_CAPABILITY_EVENT,
  type IdentityCredentialCapability,
  identityCredentialStore,
} from "@/identityCredentialStore";
import {
  SESSION_IDENTITY_EVENT,
  currentSessionIdentityStatus,
  ensureServerSession,
  reconnectServerIdentity,
  type SessionIdentityStatus,
} from "@/session";
import { openUpdateTarget } from "@/runtime";

export default function IdentityStatus(): JSX.Element | null {
  const [capability, setCapability] =
    useState<IdentityCredentialCapability | null>(null);
  const [session, setSession] = useState<SessionIdentityStatus>(() =>
    currentSessionIdentityStatus(),
  );
  const [capabilityFailed, setCapabilityFailed] = useState(false);
  const [retrying, setRetrying] = useState(false);

  const retrySession = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      await ensureServerSession(true);
    } catch {
      // Session status already carries a sanitized, user-visible failure state.
    } finally {
      setRetrying(false);
    }
  };

  const retryCapability = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      const value = await identityCredentialStore.capability();
      setCapability(value);
      setCapabilityFailed(false);
    } catch {
      setCapabilityFailed(true);
    } finally {
      setRetrying(false);
    }
  };

  const reconnectIdentity = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      await reconnectServerIdentity();
    } catch {
      // Lost identity remains fail-closed and the same explicit action stays available.
    } finally {
      setRetrying(false);
    }
  };

  useEffect(() => {
    let active = true;
    const onCapability = (event: Event) => {
      const detail = (event as CustomEvent<IdentityCredentialCapability>).detail;
      if (detail) setCapability(detail);
    };
    const onSession = (event: Event) => {
      const detail = (event as CustomEvent<SessionIdentityStatus>).detail;
      if (detail) setSession(detail);
    };
    window.addEventListener(IDENTITY_CAPABILITY_EVENT, onCapability);
    window.addEventListener(SESSION_IDENTITY_EVENT, onSession);
    void identityCredentialStore
      .capability()
      .then((value) => {
        if (active) setCapability(value);
      })
      .catch(() => {
        if (active) setCapabilityFailed(true);
      });
    return () => {
      active = false;
      window.removeEventListener(IDENTITY_CAPABILITY_EVENT, onCapability);
      window.removeEventListener(SESSION_IDENTITY_EVENT, onSession);
    };
  }, []);

  if (session.phase === "upgrade-required") {
    return (
      <Tooltip
        title={
          session.message ??
          "公共服务要求更高版本。身份续签、业务请求和 WebSocket 已停止；请先更新 EchoDesk。"
        }
      >
        <button
          type="button"
          className="identity-status is-upgrade"
          data-testid="identity-status-upgrade"
          onClick={() => void openUpdateTarget()}
          aria-label="打开 EchoDesk 更新页面"
        >
          <ShieldAlert aria-hidden="true" />
          <span>客户端需升级</span>
          <ExternalLink aria-hidden="true" />
        </button>
      </Tooltip>
    );
  }

  if (session.phase === "identity-lost") {
    return (
      <Tooltip title="自动续签已停止。恢复网络或由管理员恢复原身份后，可用同一设备凭证明确重连；EchoDesk 不会创建新的 owner。">
        <button
          type="button"
          className="identity-status is-lost"
          data-testid="identity-status-lost"
          data-retrying={retrying ? "true" : "false"}
          onClick={() => void reconnectIdentity()}
          disabled={retrying}
          aria-label="重新连接设备身份"
        >
          <ShieldAlert aria-hidden="true" />
          <span>身份失效 · 重新连接</span>
          <RefreshCw className={retrying ? "is-spinning" : ""} aria-hidden="true" />
        </button>
      </Tooltip>
    );
  }

  if (session.phase === "error") {
    const secureStorageUnavailable = session.message?.includes("安全身份存储不可用");
    const httpsRequired = session.message?.includes("HTTPS");
    return (
      <Tooltip title={session.message ?? "EchoDesk 暂时无法验证或恢复此设备身份；请检查连接或安全存储后重试。"}>
        <button
          type="button"
          className="identity-status is-error"
          data-testid="identity-status-error"
          data-retrying={retrying ? "true" : "false"}
          onClick={() => void retrySession()}
          disabled={retrying}
          aria-label="重试设备身份连接"
        >
          <ShieldAlert aria-hidden="true" />
          <span>
            {secureStorageUnavailable
              ? "安全身份存储不可用"
              : httpsRequired
                ? "身份连接需要 HTTPS"
                : "身份连接异常"}
          </span>
          <RefreshCw className={retrying ? "is-spinning" : ""} aria-hidden="true" />
        </button>
      </Tooltip>
    );
  }

  if (capabilityFailed) {
    return (
      <Tooltip title="此设备的安全身份存储不可用，公共数据身份无法可靠恢复。">
        <button
          type="button"
          className="identity-status is-lost"
          data-testid="identity-status-unavailable"
          data-retrying={retrying ? "true" : "false"}
          onClick={() => void retryCapability()}
          disabled={retrying}
          aria-label="重试安全身份存储"
        >
          <ShieldAlert aria-hidden="true" />
          <span>身份不可用</span>
          <RefreshCw className={retrying ? "is-spinning" : ""} aria-hidden="true" />
        </button>
      </Tooltip>
    );
  }

  if (capability?.persistence !== "memory-only") return null;
  return (
    <Tooltip title="当前是浏览器临时身份。刷新或关闭页面后会建立新的独立身份，原 owner 的历史数据不会自动恢复；需要长期使用时请安装 EchoDesk 客户端。">
      <span
        className="identity-status is-temporary"
        data-testid="identity-status-temporary"
        role="status"
      >
        <Clock3 aria-hidden="true" />
        <span>临时身份</span>
      </span>
    </Tooltip>
  );
}

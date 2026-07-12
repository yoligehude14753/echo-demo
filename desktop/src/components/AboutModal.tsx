/**
 * AboutModal：「关于 EchoDesk」对话框（P3.3）。
 *
 * 入口：顶栏 v0.x 徽章可点。
 *
 * 展示：
 *   - 前端版本（编译时注入，package.json）
 *   - 后端版本（运行时拉 /healthz/full backend.version）
 *   - 数据目录路径（拉 /admin/data-dir）
 *   - CHANGELOG.md / docs/INSTALL.md 链接（Electron 用 shell.openExternal，
 *     浏览器场景用 a target=_blank）
 *   - License / Repo
 */

import { useEffect, useState } from "react";
import { Modal, Spin } from "antd";
import { AudioWaveform, Github, FileText, FolderOpen, Info } from "lucide-react";
import { apiUrl, isPublicRuntime } from "@/runtime";
import { apiTransport } from "@/session";

const FRONTEND_VERSION = __APP_VERSION__;
const REPO_URL = "https://github.com/yoligehude14753/echo-demo";

interface Props {
  open: boolean;
  onClose: () => void;
}

interface HealthFullDTO {
  backend?: { version?: string; port?: number; uptime_s?: number };
}

interface DataDirDTO {
  path: string;
}

export default function AboutModal({ open, onClose }: Props): JSX.Element {
  const [backendVer, setBackendVer] = useState<string | null>(null);
  const [dataDir, setDataDir] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const publicRuntime = isPublicRuntime();

  useEffect(() => {
    if (!open) return;
    setBackendVer(null);
    setDataDir(null);
    if (publicRuntime) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const [healthRes, dirRes] = await Promise.allSettled([
          apiTransport(await apiUrl("/healthz/full"), {}, {
            timeoutMs: 12_000,
            throwHttpErrors: false,
          }),
          apiTransport(await apiUrl("/admin/data-dir"), {}, {
            timeoutMs: 12_000,
            throwHttpErrors: false,
          }),
        ]);
        if (cancelled) return;

        if (healthRes.status === "fulfilled" && healthRes.value.ok) {
          const j = (await healthRes.value.json()) as HealthFullDTO;
          setBackendVer(j.backend?.version ?? "unknown");
        } else {
          setBackendVer("unreachable");
        }
        if (dirRes.status === "fulfilled" && dirRes.value.ok) {
          const j = (await dirRes.value.json()) as DataDirDTO;
          setDataDir(j.path);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, publicRuntime]);

  return (
    <Modal
      title={
        <div className="flex items-center gap-2">
          <Info className="w-4 h-4 text-accent" />
          <span>关于 EchoDesk</span>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={460}
      destroyOnHidden
    >
      <div className="space-y-4 text-[13px]" data-testid="about-modal-body">
        <div className="flex items-center gap-3">
          <div className="w-12 h-12 rounded-xl bg-ink-900 flex items-center justify-center text-white">
            <AudioWaveform className="h-6 w-6" aria-hidden="true" />
          </div>
          <div>
            <div className="text-[15px] font-semibold text-ink-900 brand">
              EchoDesk
            </div>
            <div className="text-[11px] text-ink-500">
              本地优先的会议与办公助理
            </div>
          </div>
        </div>

        <div className="rounded border border-paper-300 bg-paper-100 p-3 space-y-1.5 text-[11px] text-ink-700">
          <div className="flex justify-between">
            <span className="text-ink-500">桌面应用</span>
            <span className="tabular-nums" data-testid="about-frontend-version">v{FRONTEND_VERSION}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-ink-500">
              {publicRuntime ? "服务模式" : "本地服务"}
            </span>
            <span className="tabular-nums" data-testid="about-backend-version">
              {publicRuntime ? (
                "公共服务"
              ) : loading && !backendVer ? (
                <Spin size="small" />
              ) : backendVer === "unreachable" ? (
                "暂时无法连接"
              ) : backendVer === "unknown" ? (
                "版本未知"
              ) : (
                `v${backendVer ?? "-"}`
              )}
            </span>
          </div>
          {dataDir && (
            <div className="flex justify-between items-center gap-2">
              <span className="text-ink-500 shrink-0">数据目录</span>
              <span className="truncate text-right font-mono" title={dataDir}>
                {dataDir}
              </span>
            </div>
          )}
        </div>

        <ul className="text-[12px] text-ink-600 space-y-1.5">
          <li className="flex items-center gap-2">
            <FileText className="w-3.5 h-3.5 text-ink-400" />
            <a
              className="text-accent hover:underline"
              href={`${REPO_URL}/blob/main/CHANGELOG.md`}
              target="_blank"
              rel="noreferrer"
              data-testid="about-changelog-link"
            >
              更新日志（CHANGELOG.md）
            </a>
          </li>
          <li className="flex items-center gap-2">
            <FolderOpen className="w-3.5 h-3.5 text-ink-400" />
            <a
              className="text-accent hover:underline"
              href={`${REPO_URL}/blob/main/docs/INSTALL.md`}
              target="_blank"
              rel="noreferrer"
              data-testid="about-install-link"
            >
              安装与卸载指南（docs/INSTALL.md）
            </a>
          </li>
          <li className="flex items-center gap-2">
            <Github className="w-3.5 h-3.5 text-ink-400" />
            <a
              className="text-accent hover:underline"
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
            >
              源码仓库
            </a>
          </li>
        </ul>

        <div className="text-[10px] text-ink-400 text-center pt-1">
          © 2026 EchoDesk · 本地优先 · 客户端不内置模型密钥
        </div>
      </div>
    </Modal>
  );
}

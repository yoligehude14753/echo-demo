/**
 * SettingsPanel · Phase 2 P2.5 + P2.6 frontend
 *
 * 抽屉式设置面板，三个能力：
 *  - 显示 ~/.echodesk/ 数据目录大小 + 子目录 breakdown
 *  - 导出诊断包 zip（一键下载）
 *  - 重置 speaker registry（保留 transcript）
 *
 * 由 App.tsx header 齿轮按钮触发；用 antd Drawer 从右边滑出，不阻断主工作流。
 *
 * 数据接口（依赖 phase2-admin-diagnostics backend PR）：
 *  GET  /admin/data-dir
 *  GET  /admin/diagnostics/export
 *  POST /admin/speakers/reset
 */

import { Drawer, Button, Modal, message, Spin, Tooltip, Input, Form, Tag } from "antd";
import {
  Database,
  Download,
  Folder,
  FolderOpen,
  FolderPlus,
  RefreshCw,
  Users,
  AlertTriangle,
  Server,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  workspaceAddDir,
  workspaceRemoveDir,
  workspaceScan,
  workspaceStatus,
} from "@/api";
import {
  DEFAULT_ANDROID_BACKEND_BASE,
  apiUrl,
  configuredBackendBase,
  setStoredBackendBase,
} from "@/runtime";

interface DataDirBreakdown {
  db: number;
  storage: number;
  rag_index: number;
  logs: number;
  skill_build: number;
}

interface DataDirResponse {
  path: string;
  exists: boolean;
  size_bytes: number;
  breakdown: DataDirBreakdown;
}

interface SpeakerResetResponse {
  speakers_deleted: number;
  segments_cleared: number;
  diarizer_reset: boolean;
}

interface RemoteField {
  key: string;
  value: string;
  sensitive: boolean;
  source: "default" | "user";
}

interface RemoteSettingsResponse {
  config_path: string;
  fields: RemoteField[];
}

interface RemoteFieldMeta {
  label: string;
  hint: string;
  placeholder?: string;
}

// 字段顺序 + 文案，跟后端 _REMOTE_FIELDS 对齐
const REMOTE_FIELD_META: Record<string, RemoteFieldMeta> = {
  llm_main_base_url: {
    label: "主 LLM Base URL",
    hint: "Yunwu / OpenAI 兼容端点；默认 https://yunwu.ai/v1",
    placeholder: "https://yunwu.ai/v1",
  },
  yunwu_open_key: {
    label: "主 LLM API Key",
    hint: "Yunwu key（sk- 开头）；脱敏显示，留空不修改",
    placeholder: "sk-...",
  },
  llm_fast_base_url: {
    label: "快速 LLM Base URL",
    hint: "qwen3.5-9b-local (eight :7860)；用于 intent 分类等低延时任务",
    placeholder: "http://100.76.3.59:7860/v1",
  },
  stt_firered_url: {
    label: "STT URL",
    hint: "FireRedASR2-AED (eight :8090)",
    placeholder: "http://100.76.3.59:8090",
  },
  tts_qwen3_url: {
    label: "TTS URL",
    hint: "faster-qwen3-tts (eight :8094)",
    placeholder: "http://100.76.3.59:8094",
  },
  tts_qwen3_voice: {
    label: "TTS 音色",
    hint: "CustomVoice 名（aiden / alice / ...）",
    placeholder: "aiden",
  },
  tavily_api_key: {
    label: "Tavily API Key",
    hint: "Web 检索；脱敏显示，留空不修改",
    placeholder: "tvly-...",
  },
};

interface WorkspaceStatusDTO {
  configured_dirs: string[];
  authorized_dirs: string[];
  n_indexed: number;
  max_file_mb: number;
  scan_on_startup: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** P3.1：让用户在设置里"重新看一次引导"。可选，缺省时不显示按钮。 */
  onReplayOnboarding?: () => void;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

const BREAKDOWN_LABELS: Array<{
  key: keyof DataDirBreakdown;
  label: string;
  hint: string;
}> = [
  { key: "db", label: "数据库", hint: "echodesk.db (会议/段/说话人)" },
  { key: "storage", label: "音频/产物", hint: "录音 wav + @生成 的 PPT/Word/Excel/HTML" },
  { key: "rag_index", label: "RAG 索引", hint: "BM25 倒排索引" },
  { key: "logs", label: "日志", hint: "backend.log 按天 rotate，保留 14 天" },
  { key: "skill_build", label: "Skill 工作目录", hint: "@生成 临时构建目录" },
];

export default function SettingsPanel({
  open,
  onClose,
  onReplayOnboarding,
}: Props): JSX.Element {
  const [dataDir, setDataDir] = useState<DataDirResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [diagBusy, setDiagBusy] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [remote, setRemote] = useState<RemoteSettingsResponse | null>(null);
  const [remoteBusy, setRemoteBusy] = useState(false);
  const [needsRestart, setNeedsRestart] = useState(false);
  const [adminUnavailable, setAdminUnavailable] = useState(false);
  const [form] = Form.useForm<Record<string, string>>();
  const [backendBaseDraft, setBackendBaseDraft] = useState("");
  // P4-fix-rag-chat：工作区目录配置
  const [ws, setWs] = useState<WorkspaceStatusDTO | null>(null);
  const [wsBusy, setWsBusy] = useState(false);
  const [wsScanBusy, setWsScanBusy] = useState(false);

  const refreshDataDir = useCallback(async () => {
    setLoading(true);
    try {
      const url = await apiUrl("/admin/data-dir");
      const res = await fetch(url);
      if (res.status === 403) {
        setAdminUnavailable(true);
        setDataDir(null);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as DataDirResponse;
      setAdminUnavailable(false);
      setDataDir(json);
    } catch (e) {
      message.error(`读取数据目录失败：${(e as Error).message}`);
      setDataDir(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshRemote = useCallback(async () => {
    try {
      const url = await apiUrl("/admin/settings/remote");
      const res = await fetch(url);
      if (res.status === 403) {
        setAdminUnavailable(true);
        setRemote(null);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as RemoteSettingsResponse;
      setAdminUnavailable(false);
      setRemote(json);
      // 重置表单：sensitive 字段不显示原值（脱敏值仅作 placeholder），
      // 用户留空就不修改；非 sensitive 字段直接拿明文当初值
      const initial: Record<string, string> = {};
      for (const f of json.fields) {
        initial[f.key] = f.sensitive ? "" : f.value;
      }
      form.setFieldsValue(initial);
    } catch (e) {
      message.error(`读取远端配置失败：${(e as Error).message}`);
      setRemote(null);
    }
  }, [form]);

  const refreshWorkspace = useCallback(async () => {
    try {
      const json = await workspaceStatus();
      setWs(json);
    } catch (e) {
      message.error(`读取工作区状态失败：${(e as Error).message}`);
      setWs(null);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void refreshDataDir();
      void refreshRemote();
      void refreshWorkspace();
      setBackendBaseDraft(configuredBackendBase() ?? DEFAULT_ANDROID_BACKEND_BASE);
    }
  }, [open, refreshDataDir, refreshRemote, refreshWorkspace]);

  const onAddWorkspaceDir = useCallback(async () => {
    // 优先用 Electron dialog；浏览器/纯 dev 模式回退到 prompt() 让用户手填路径
    let picked: string | null | undefined;
    try {
      if (window.echo?.pickDirectory) {
        picked = await window.echo.pickDirectory({
          defaultPath: ws?.configured_dirs?.[0],
        });
      } else {
        const v = window.prompt(
          "输入要加入工作区的目录绝对路径（如 /Users/you/Documents）：",
          ws?.configured_dirs?.[0] ?? "",
        );
        picked = v && v.trim() ? v.trim() : null;
      }
    } catch (e) {
      message.error(`选择目录失败：${(e as Error).message}`);
      return;
    }
    if (!picked) return; // 用户取消
    setWsBusy(true);
    try {
      const r = await workspaceAddDir(picked);
      if (r.added) {
        message.success(`已加入：${r.path}（后台扫描索引中…）`);
      } else {
        message.info("该目录已在工作区，无需重复添加");
      }
      await refreshWorkspace();
    } catch (e) {
      message.error(`添加失败：${(e as Error).message}`);
    } finally {
      setWsBusy(false);
    }
  }, [ws, refreshWorkspace]);

  const onRemoveWorkspaceDir = useCallback(
    async (dir: string) => {
      Modal.confirm({
        title: "移除工作区目录？",
        icon: <AlertTriangle className="w-4 h-4 text-amber-500" />,
        content: (
          <div className="text-[12px] leading-relaxed">
            将移除 <span className="font-mono">{dir}</span>
            。该目录下已索引的文件会在下次扫描时清理（保留其他来源的 RAG 数据）。
          </div>
        ),
        okText: "移除",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: async () => {
          setWsBusy(true);
          try {
            const r = await workspaceRemoveDir(dir);
            if (r.removed) {
              message.success(`已移除：${dir}`);
            }
            await refreshWorkspace();
          } catch (e) {
            message.error(`移除失败：${(e as Error).message}`);
          } finally {
            setWsBusy(false);
          }
        },
      });
    },
    [refreshWorkspace],
  );

  const onRescanWorkspace = useCallback(async () => {
    setWsScanBusy(true);
    try {
      const r = await workspaceScan();
      message.success(
        `扫描完成：新增 ${r.n_added} · 更新 ${r.n_updated} · 跳过 ${r.n_skipped}`,
      );
      await refreshWorkspace();
    } catch (e) {
      message.error(`扫描失败：${(e as Error).message}`);
    } finally {
      setWsScanBusy(false);
    }
  }, [refreshWorkspace]);

  const onOpenDataDir = async () => {
    if (!dataDir?.path) return;
    try {
      await navigator.clipboard.writeText(dataDir.path);
      message.success(`已复制路径到剪贴板：${dataDir.path}`);
    } catch {
      message.info(`数据目录：${dataDir.path}`);
    }
  };

  const onDownloadDiagnostics = async () => {
    setDiagBusy(true);
    try {
      const url = await apiUrl("/admin/diagnostics/export");
      const a = document.createElement("a");
      a.href = url;
      a.download = `echodesk-diag-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      message.success("诊断包下载中…");
    } catch (e) {
      message.error(`导出失败：${(e as Error).message}`);
    } finally {
      setDiagBusy(false);
    }
  };

  const onSaveRemote = async (values: Record<string, string>) => {
    if (!remote) return;
    // 只 PATCH 有真实输入的字段：sensitive 字段空字符串表示"不改"；
    // 非 sensitive 字段如果跟当前明文一致也跳过（避免空写）
    const original = new Map(remote.fields.map((f) => [f.key, f]));
    const updates: Record<string, string> = {};
    for (const [k, v] of Object.entries(values)) {
      const meta = original.get(k);
      if (!meta) continue;
      if (meta.sensitive) {
        if (v && v.length > 0) updates[k] = v;
      } else {
        if (v !== meta.value) updates[k] = v;
      }
    }
    if (Object.keys(updates).length === 0) {
      message.info("没有变更");
      return;
    }
    setRemoteBusy(true);
    try {
      const url = await apiUrl("/admin/settings/remote");
      const res = await fetch(url, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(`HTTP ${res.status}: ${detail.slice(0, 200)}`);
      }
      const json = (await res.json()) as {
        written_keys: string[];
        restart_required: boolean;
      };
      message.success(
        `已写入 ${json.written_keys.length} 项${json.restart_required ? "，需重启后端生效" : ""}`,
      );
      setNeedsRestart(json.restart_required);
      void refreshRemote();
    } catch (e) {
      message.error(`保存失败：${(e as Error).message}`);
    } finally {
      setRemoteBusy(false);
    }
  };

  const onRestartBackend = async () => {
    if (!window.echo?.manualRestartBackend) {
      message.warning("仅 Electron 模式可一键重启；dev server 请手动重启 backend");
      return;
    }
    try {
      await window.echo.manualRestartBackend();
      message.success("已发送重启请求");
      setNeedsRestart(false);
    } catch (e) {
      message.error(`重启失败：${(e as Error).message}`);
    }
  };

  const onSaveBackendBase = () => {
    const saved = setStoredBackendBase(backendBaseDraft);
    setBackendBaseDraft(saved ?? DEFAULT_ANDROID_BACKEND_BASE);
    message.success(saved ? `后端地址已保存：${saved}` : "已恢复默认后端地址");
  };

  const remoteFieldOrder = useMemo(
    () => (remote?.fields ?? []).map((f) => f.key),
    [remote],
  );

  const onResetSpeakers = () => {
    Modal.confirm({
      title: "重置说话人？",
      icon: <AlertTriangle className="w-4 h-4 text-amber-500" />,
      content: (
        <div className="text-[12px] leading-relaxed">
          将清空 <b>所有说话人识别数据</b>（speakers / centroid），但
          <b>保留所有转写文字</b>。下次录音时 diarizer 将从头学习。
          <br />
          <br />
          适用场景：speaker registry 被噪音污染（"说话人 86" 实际只 3 人）。
        </div>
      ),
      okText: "确认重置",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        setResetBusy(true);
        try {
          const url = await apiUrl("/admin/speakers/reset");
          const res = await fetch(url, { method: "POST" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = (await res.json()) as SpeakerResetResponse;
          message.success(
            `已重置：清空 ${json.speakers_deleted} 个说话人 · 清理 ${json.segments_cleared} 段引用 · diarizer ${json.diarizer_reset ? "已重置" : "未重置"}`,
          );
          void refreshDataDir();
        } catch (e) {
          message.error(`重置失败：${(e as Error).message}`);
        } finally {
          setResetBusy(false);
        }
      },
    });
  };

  return (
    <Drawer
      title={<span className="text-[14px] font-semibold">设置</span>}
      placement="right"
      width={420}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      <div className="space-y-5 text-[13px]">
        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Database className="w-4 h-4" />
            <span>数据</span>
            <Tooltip title="刷新">
              <button
                type="button"
                onClick={() => void refreshDataDir()}
                className="ml-auto text-ink-400 hover:text-ink-700"
                aria-label="刷新数据目录"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
              </button>
            </Tooltip>
          </div>
          <div className="bg-paper-150 rounded-md p-3 space-y-2">
            {adminUnavailable ? (
              <div className="text-[12px] text-ink-500 leading-relaxed">
                公网 demo backend 不开放本机数据目录、日志和诊断导出。Mac / Windows
                桌面端本机 backend 可继续使用这些管理功能。
              </div>
            ) : loading && !dataDir ? (
              <Spin size="small" />
            ) : !dataDir ? (
              <div className="text-ink-400 text-[12px]">读取失败</div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] text-ink-600 truncate">
                    {dataDir.path}
                  </span>
                  <Button
                    size="small"
                    type="text"
                    icon={<FolderOpen className="w-3.5 h-3.5" />}
                    onClick={() => void onOpenDataDir()}
                    data-testid="open-data-dir"
                  >
                    复制路径
                  </Button>
                </div>
                <div className="flex items-center justify-between text-[12px] py-1 border-b border-paper-300">
                  <span className="font-medium">总占用</span>
                  <span className="font-mono text-accent">
                    {fmtBytes(dataDir.size_bytes)}
                  </span>
                </div>
                {BREAKDOWN_LABELS.map(({ key, label, hint }) => (
                  <div
                    key={key}
                    className="flex items-center justify-between text-[11px] text-ink-600"
                    title={hint}
                  >
                    <span>{label}</span>
                    <span className="font-mono">
                      {fmtBytes(dataDir.breakdown[key] ?? 0)}
                    </span>
                  </div>
                ))}
              </>
            )}
          </div>
        </section>

        {!adminUnavailable && (
          <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Server className="w-4 h-4" />
            <span>远端服务</span>
            {remote && (
              <span className="ml-auto text-[10px] text-ink-400 truncate max-w-[180px]">
                {remote.config_path}
              </span>
            )}
          </div>
          {!remote ? (
            <Spin size="small" />
          ) : (
            <Form
              form={form}
              layout="vertical"
              onFinish={onSaveRemote}
              autoComplete="off"
              data-testid="remote-settings-form"
            >
              {remoteFieldOrder.map((key) => {
                const meta = REMOTE_FIELD_META[key];
                const fieldDto = remote.fields.find((f) => f.key === key);
                if (!meta || !fieldDto) return null;
                return (
                  <Form.Item
                    key={key}
                    name={key}
                    label={
                      <div className="text-[12px] flex items-center gap-1.5">
                        <span>{meta.label}</span>
                        {fieldDto.source === "user" && (
                          <span className="text-[10px] px-1 rounded bg-accent/10 text-accent">
                            user.json
                          </span>
                        )}
                        {fieldDto.sensitive && (
                          <Tooltip title={`当前值（脱敏）：${fieldDto.value || "（空）"}`}>
                            <span className="text-[10px] text-ink-400 cursor-help">
                              [脱敏]
                            </span>
                          </Tooltip>
                        )}
                      </div>
                    }
                    extra={<span className="text-[11px] text-ink-400">{meta.hint}</span>}
                  >
                    {fieldDto.sensitive ? (
                      <Input.Password
                        placeholder={meta.placeholder ?? ""}
                        size="small"
                        autoComplete="off"
                      />
                    ) : (
                      <Input placeholder={meta.placeholder ?? ""} size="small" />
                    )}
                  </Form.Item>
                );
              })}
              <div className="flex gap-2">
                <Button
                  type="primary"
                  htmlType="submit"
                  loading={remoteBusy}
                  size="small"
                  data-testid="save-remote-settings"
                >
                  保存
                </Button>
                <Button size="small" onClick={() => void refreshRemote()}>
                  重置
                </Button>
                {needsRestart && (
                  <Button
                    size="small"
                    type="dashed"
                    onClick={() => void onRestartBackend()}
                    data-testid="restart-backend-after-config"
                  >
                    重启 backend 生效
                  </Button>
                )}
              </div>
            </Form>
          )}
          </section>
        )}

        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Server className="w-4 h-4" />
            <span>移动端连接</span>
          </div>
          <div className="bg-paper-150 rounded-md p-3 space-y-2">
            <Input
              size="small"
              value={backendBaseDraft}
              onChange={(e) => setBackendBaseDraft(e.target.value)}
              placeholder={DEFAULT_ANDROID_BACKEND_BASE}
              data-testid="mobile-backend-base"
            />
            <div className="flex gap-2">
              <Button
                size="small"
                type="primary"
                onClick={onSaveBackendBase}
                data-testid="save-mobile-backend-base"
              >
                保存地址
              </Button>
              <Button
                size="small"
                onClick={() => {
                  setBackendBaseDraft("");
                  setStoredBackendBase("");
                  message.success("已恢复默认后端地址");
                }}
              >
                恢复默认
              </Button>
            </div>
            <div className="text-[11px] text-ink-500 leading-relaxed">
              Android / TV 默认连接 EchoDesk 公网 demo backend：
              <span className="font-mono ml-1">{DEFAULT_ANDROID_BACKEND_BASE}</span>。
              内网演示或开发调试时，可临时改成电脑局域网地址，例如
              <span className="font-mono ml-1">http://10.10.12.32:8769</span>。
            </div>
          </div>
        </section>

        <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Folder className="w-4 h-4" />
            <span>工作区目录</span>
            <Tooltip title="刷新">
              <button
                type="button"
                onClick={() => void refreshWorkspace()}
                className="ml-auto text-ink-400 hover:text-ink-700"
                aria-label="刷新工作区状态"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </Tooltip>
          </div>
          <div className="bg-paper-150 rounded-md p-3 space-y-2">
            {!ws ? (
              <Spin size="small" />
            ) : (
              <>
                <div className="text-[11px] text-ink-500 leading-relaxed">
                  EchoDesk 会扫描这些目录下的可索引文件（PDF / Word / Excel / PPT /
                  Markdown / TXT 等），自动入库 RAG，让"@查 / 提问"能覆盖整个文件夹。
                  <br />
                  当前已索引 <span className="font-mono text-accent">{ws.n_indexed}</span> 个文件
                  · 单文件上限 {ws.max_file_mb} MB
                </div>
                {ws.configured_dirs.length === 0 ? (
                  <div className="text-[12px] text-ink-400 italic">
                    （暂未配置任何目录；点下方按钮添加）
                  </div>
                ) : (
                  <div className="space-y-1">
                    {ws.configured_dirs.map((d) => {
                      const authorized = ws.authorized_dirs.includes(d);
                      return (
                        <div
                          key={d}
                          className="flex items-center gap-1.5 text-[11px]"
                          data-testid="workspace-dir-row"
                        >
                          <Folder className="w-3 h-3 text-ink-400 shrink-0" />
                          <span
                            className="font-mono text-ink-700 truncate flex-1"
                            title={d}
                          >
                            {d}
                          </span>
                          {!authorized && (
                            <Tag color="warning" className="!m-0 !text-[10px]">
                              未访问
                            </Tag>
                          )}
                          <Tooltip title="从工作区移除">
                            <button
                              type="button"
                              onClick={() => void onRemoveWorkspaceDir(d)}
                              disabled={wsBusy}
                              className="text-ink-400 hover:text-red-500 disabled:opacity-40"
                              aria-label={`移除 ${d}`}
                              data-testid={`workspace-remove-dir-${d}`}
                            >
                              <Trash2 className="w-3 h-3" />
                            </button>
                          </Tooltip>
                        </div>
                      );
                    })}
                  </div>
                )}
                <div className="flex gap-1.5 pt-1.5">
                  <Button
                    size="small"
                    type="primary"
                    icon={<FolderPlus className="w-3.5 h-3.5" />}
                    loading={wsBusy}
                    onClick={() => void onAddWorkspaceDir()}
                    data-testid="workspace-add-dir"
                  >
                    添加目录
                  </Button>
                  <Button
                    size="small"
                    icon={<RefreshCw className="w-3.5 h-3.5" />}
                    loading={wsScanBusy}
                    onClick={() => void onRescanWorkspace()}
                    disabled={ws.configured_dirs.length === 0}
                    data-testid="workspace-rescan"
                  >
                    立即重扫
                  </Button>
                </div>
              </>
            )}
          </div>
        </section>

        {!adminUnavailable && (
          <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Download className="w-4 h-4" />
            <span>诊断</span>
          </div>
          <Button
            block
            icon={<Download className="w-3.5 h-3.5" />}
            loading={diagBusy}
            onClick={() => void onDownloadDiagnostics()}
            data-testid="download-diagnostics"
          >
            导出诊断包 (.zip)
          </Button>
          <div className="text-[11px] text-ink-500 mt-1.5 leading-relaxed">
            包含：最近 7 天 backend log（≤5MB/文件）· 配置（API key
            已脱敏）· DB schema · 远程探针历史。报 bug 时把这个 zip 发给我们。
          </div>
          </section>
        )}

        {!adminUnavailable && (
          <section>
          <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
            <Users className="w-4 h-4" />
            <span>说话人管理</span>
          </div>
          <Button
            block
            danger
            icon={<AlertTriangle className="w-3.5 h-3.5" />}
            loading={resetBusy}
            onClick={onResetSpeakers}
            data-testid="reset-speakers"
          >
            重置说话人（保留转写）
          </Button>
          <div className="text-[11px] text-ink-500 mt-1.5 leading-relaxed">
            清空 speakers 表和 diarizer 内存，<b>转写文字保留</b>。下次录音
            重新学习。常用于 speaker 数量被噪音污染时。
          </div>
          </section>
        )}

        {onReplayOnboarding && (
          <section>
            <div className="flex items-center gap-2 mb-2 text-ink-700 font-medium">
              <RefreshCw className="w-4 h-4" />
              <span>引导</span>
            </div>
            <Button
              block
              onClick={() => {
                onReplayOnboarding();
                onClose();
              }}
              data-testid="replay-onboarding"
            >
              重新看一次引导
            </Button>
            <div className="text-[11px] text-ink-500 mt-1.5">
              重新打开欢迎引导（数据目录、麦克风权限、@命令使用提示）。
            </div>
          </section>
        )}

        <section className="pt-3 border-t border-paper-300">
          <div className="text-[11px] text-ink-500 leading-relaxed">
            <div>EchoDesk · 独立桌面 AI 会议助手</div>
            <div className="mt-1">
              配置文件：
              <span className="font-mono text-ink-600">~/.echodesk/config.json</span>
            </div>
            <div className="mt-1">
              卸载：终端运行
              <span className="font-mono text-ink-600 ml-1">
                bash scripts/install-backend.sh --uninstall
              </span>
            </div>
          </div>
        </section>
      </div>
    </Drawer>
  );
}

/**
 * OnboardingModal：首次启动的 3 步引导（P3.1）。
 *
 * 步骤：
 *   1. 欢迎 — 简介 EchoDesk 能做什么 + 数据存放位置（~/.echodesk/）
 *   2. 麦克风权限 — 自动探测；引导用户点"允许录音"或"打开系统设置"
 *   3. 完成 — 简单提示 @ 命令栏 + 录音按钮位置
 *
 * 持久化：完成或跳过都 markCompleted() 写 localStorage，下次启动不再弹。
 * 用户后续想重看，在 SettingsPanel 触发 resetForDebug() 即可。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button, Modal, Steps } from "antd";
import { CheckCircle2, FolderOpen, Mic, Sparkles } from "lucide-react";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { apiUrl } from "@/runtime";
import { apiTransport } from "@/session";
import { isNativeMobile } from "@/runtime";

type StepKey = "welcome" | "mic" | "done";

const STEPS: StepKey[] = ["welcome", "mic", "done"];

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function OnboardingModal({ open, onClose }: Props): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const [stepIdx, setStepIdx] = useState(0);
  const stepKey: StepKey = STEPS[stepIdx] ?? "welcome";
  const wasOpenRef = useRef(open);
  const handledOriginRevision = useRef(backendOriginRevision);

  const [dataDirPath, setDataDirPath] = useState<string | null>(null);
  const [micState, setMicState] = useState<"unknown" | "granted" | "denied" | "prompt">(
    "unknown",
  );
  const [requesting, setRequesting] = useState(false);

  useEffect(() => {
    if (handledOriginRevision.current === backendOriginRevision) return;
    handledOriginRevision.current = backendOriginRevision;
    setStepIdx(0);
    setDataDirPath(null);
    setMicState("unknown");
    setRequesting(false);
    onClose();
  }, [backendOriginRevision, onClose]);

  // OnboardingModal 本身始终挂载；AntD 只会销毁 Modal 内部节点，因此步骤 state
  // 不会随着弹窗关闭自动清空。只在“已关闭 → 再次打开”的边沿回到欢迎页，
  // 避免首次打开或用户切换步骤时被 effect 意外重置。
  useLayoutEffect(() => {
    const isReopening = open && !wasOpenRef.current;
    wasOpenRef.current = open;
    if (isReopening) setStepIdx(0);
  }, [open]);

  // 拉数据目录路径（让用户知道数据存在哪）
  useEffect(() => {
    if (!open) return;
    let alive = true;
    const originGeneration = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && isCurrent(originGeneration) && !controller.signal.aborted;
    (async () => {
      try {
        const u = await apiUrl("/admin/data-dir");
        const r = await apiTransport(
          u,
          { signal: controller.signal },
          { timeoutMs: 8_000, throwHttpErrors: false },
        );
        if (!r.ok) return;
        const d = (await r.json()) as { path?: string };
        if (canCommit() && d.path) setDataDirPath(d.path);
      } catch {
        /* 让用户看到 path 是 nice-to-have，失败就显示 ~/.echodesk/ */
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [captureGeneration, isCurrent, open, registerAbortController]);

  // 拉麦克风权限初值（mic 步骤进入时再查一次以拿最新值）
  useEffect(() => {
    if (!open) return;
    let alive = true;
    const originGeneration = captureGeneration();
    (async () => {
      try {
        const s = await probeMicState();
        if (alive && isCurrent(originGeneration)) setMicState(s);
      } catch {
        if (alive && isCurrent(originGeneration)) setMicState("unknown");
      }
    })();
    return () => {
      alive = false;
    };
  }, [captureGeneration, isCurrent, open, stepIdx]);

  const onRequestMic = async () => {
    const originGeneration = captureGeneration();
    setRequesting(true);
    try {
      // Electron: askForMediaAccess 触发系统弹窗（macOS）；其它环境走 getUserMedia
      if (window.echo?.requestMic) {
        const ok = await window.echo.requestMic();
        if (isCurrent(originGeneration)) {
          setMicState(ok ? "granted" : "denied");
        }
      } else {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((t) => t.stop());
        if (isCurrent(originGeneration)) {
          setMicState("granted");
        }
      }
    } catch {
      if (isCurrent(originGeneration)) setMicState("denied");
    } finally {
      if (isCurrent(originGeneration)) setRequesting(false);
    }
  };

  const onOpenSysPrefs = async () => {
    if (window.echo?.openMicSystemPrefs) {
      await window.echo.openMicSystemPrefs();
    }
  };

  const next = () => {
    if (stepIdx >= STEPS.length - 1) onClose();
    else setStepIdx(stepIdx + 1);
  };
  const prev = () => {
    if (stepIdx > 0) setStepIdx(stepIdx - 1);
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      closable={false}
      maskClosable={false}
      footer={null}
      width={520}
      title={null}
      destroyOnHidden
    >
      <div className="py-2">
        <Steps
          current={stepIdx}
          size="small"
          items={[
            { title: "欢迎" },
            { title: "麦克风" },
            { title: "完成" },
          ]}
        />

        <div className="mt-5 min-h-[200px]">
          {stepKey === "welcome" && (
            <WelcomeStep dataDirPath={dataDirPath} />
          )}
          {stepKey === "mic" && (
            <MicStep
              state={micState}
              requesting={requesting}
              onRequest={onRequestMic}
              onOpenSysPrefs={onOpenSysPrefs}
            />
          )}
          {stepKey === "done" && <DoneStep />}
        </div>

        <div className="mt-4 flex items-center justify-between">
          <button
            type="button"
            onClick={onClose}
            className="text-[12px] text-ink-400 hover:text-ink-600"
            data-testid="onboarding-skip"
          >
            跳过
          </button>
          <div className="flex gap-2">
            {stepIdx > 0 && (
              <Button onClick={prev} data-testid="onboarding-prev">
                上一步
              </Button>
            )}
            <Button
              type="primary"
              onClick={next}
              data-testid="onboarding-next"
              disabled={stepKey === "mic" && requesting}
            >
              {stepIdx === STEPS.length - 1 ? "开始使用" : "下一步"}
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function WelcomeStep({ dataDirPath }: { dataDirPath: string | null }): JSX.Element {
  return (
    <div className="space-y-3 text-[13px] text-ink-700">
      <div className="flex items-center gap-2 text-base font-medium text-ink-900">
        <Sparkles className="w-4 h-4 text-accent" />
        欢迎来到 EchoDesk
      </div>
      <div className="text-ink-600">
        EchoDesk 是一个本地优先的会议与办公助理：环境音转写、会议纪要、
        基于工作区的检索问答、文档/PPT/Excel 生成都跑在你自己的电脑上。
      </div>
      <div className="rounded border border-paper-300 bg-paper-100 p-3 text-[12px]">
        <div className="flex items-center gap-1.5 font-medium mb-1">
          <FolderOpen className="w-3.5 h-3.5 text-ink-500" />
          数据存放位置
        </div>
        <div className="text-ink-500 font-mono text-[11px] break-all">
          {dataDirPath ?? "~/.echodesk/"}
        </div>
        <div className="text-ink-400 mt-1.5">
          会议数据库、录音、知识库索引、日志全部都在这里。可在「设置 → 数据」
          里查看占用、导出诊断信息或卸载。
        </div>
      </div>
    </div>
  );
}

function MicStep({
  state,
  requesting,
  onRequest,
  onOpenSysPrefs,
}: {
  state: "unknown" | "granted" | "denied" | "prompt";
  requesting: boolean;
  onRequest: () => void;
  onOpenSysPrefs: () => void;
}): JSX.Element {
  return (
    <div className="space-y-3 text-[13px] text-ink-700">
      <div className="flex items-center gap-2 text-base font-medium text-ink-900">
        <Mic className="w-4 h-4 text-accent" />
        授权麦克风
      </div>
      <div className="text-ink-600">
        EchoDesk 需要麦克风权限才能转写会议。所有音频只发送给你配置的
        语音识别服务。
      </div>

      <div className="rounded border border-paper-300 bg-paper-100 p-3 text-[12px]">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-ink-500">当前权限</span>
          <span
            className={
              state === "granted"
                ? "text-accent font-medium"
                : state === "denied"
                  ? "text-err font-medium"
                  : "text-amber-500 font-medium"
            }
            data-testid="onboarding-mic-state"
          >
            {state === "granted"
              ? "已允许"
              : state === "denied"
                ? "已拒绝"
                : state === "prompt"
                  ? "尚未授权"
                  : "未知"}
          </span>
        </div>
        {state === "granted" && (
          <div className="flex items-center gap-1.5 text-accent text-[12px]">
            <CheckCircle2 className="w-3.5 h-3.5" />
            可以继续到下一步
          </div>
        )}
        {(state === "prompt" || state === "unknown") && (
          <Button
            type="primary"
            size="small"
            onClick={onRequest}
            loading={requesting}
            data-testid="onboarding-mic-request"
          >
            允许麦克风
          </Button>
        )}
        {state === "denied" && (
          <div className="space-y-1.5">
            <div className="text-err text-[12px]">
              系统已记住"拒绝"。请到 系统设置 → 隐私与安全 → 麦克风 勾选 EchoDesk。
            </div>
            {window.echo?.openMicSystemPrefs && (
              <Button
                size="small"
                onClick={onOpenSysPrefs}
                data-testid="onboarding-mic-open-prefs"
              >
                打开系统设置
              </Button>
            )}
          </div>
        )}
      </div>

      <div className="text-ink-400 text-[11px]">
        提示：跳过也没关系，第一次按下录音键时系统还会再弹一次。
      </div>
    </div>
  );
}

function DoneStep(): JSX.Element {
  const android = isNativeMobile();
  return (
    <div className="space-y-3 text-[13px] text-ink-700">
      <div className="flex items-center gap-2 text-base font-medium text-ink-900">
        <CheckCircle2 className="w-4 h-4 text-accent" />
        准备就绪
      </div>
      <div className="text-ink-600">三个关键交互点：</div>
      <ul className="list-disc pl-5 space-y-1.5 text-[12px] text-ink-600">
        <li>
          在底部输入问题，或直接描述要生成的文档、表格和演示文稿
        </li>
        <li>
          {android
            ? "点击「开始会议」后选择单端或多端收音；确认前不会启用麦克风"
            : "点击「开始会议」保存本次记录；不开始会议时也会持续显示实时转写"}
        </li>
        <li>
          右上「设置」可管理知识库、数据占用、诊断包和说话人
        </li>
      </ul>
      <div className="text-ink-400 text-[11px]">
        随时可以在 设置 → 重新看一次引导 回到这里。
      </div>
    </div>
  );
}

async function probeMicState(): Promise<"unknown" | "granted" | "denied" | "prompt"> {
  // 优先用 Electron 的 systemPreferences（更准确，区分 not-determined）
  if (window.echo?.getMicStatus) {
    const s = await window.echo.getMicStatus();
    if (s === "granted") return "granted";
    if (s === "denied" || s === "restricted") return "denied";
    if (s === "not-determined") return "prompt";
    // fallthrough to navigator
  }
  if (typeof navigator !== "undefined" && navigator.permissions) {
    try {
      const r = await navigator.permissions.query({
        name: "microphone" as PermissionName,
      });
      if (r.state === "granted") return "granted";
      if (r.state === "denied") return "denied";
      return "prompt";
    } catch {
      return "unknown";
    }
  }
  return "unknown";
}

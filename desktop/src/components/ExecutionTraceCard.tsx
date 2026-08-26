import { useMemo, useState } from "react";
import { Modal } from "antd";
import {
  CheckCircle2,
  Circle,
  Clock3,
  Code2,
  Download,
  ExternalLink,
  FileCode2,
  Globe2,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react";
import { artifactDownloadUrl } from "@/api";
import AuthenticatedDownloadLink from "@/components/AuthenticatedDownloadLink";
import ArtifactPreviewModal from "@/components/ArtifactPreviewModal";
import { modelDisplayName } from "@/lib/modelDisplay";
import type { ProcessTrace, ProcessTraceStep } from "@/lib/processTrace";
import type { GeneratedArtifact } from "@/types";

interface ExecutionTraceCardProps {
  trace: ProcessTrace;
}

function statusLabel(status: ProcessTrace["status"]): string {
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "未完成";
  return "处理中";
}

function stepIcon(step: ProcessTraceStep): JSX.Element {
  if (step.state === "done") {
    return <CheckCircle2 className="echodesk-process-step-icon is-done" aria-hidden="true" />;
  }
  if (step.state === "failed") {
    return <XCircle className="echodesk-process-step-icon is-failed" aria-hidden="true" />;
  }
  if (step.state === "running") {
    return <Loader2 className="echodesk-process-step-icon is-running" aria-hidden="true" />;
  }
  return <Circle className="echodesk-process-step-icon is-pending" aria-hidden="true" />;
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "大小待确认";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function safeArtifactUrl(artifact: GeneratedArtifact | null | undefined): string {
  if (!artifact?.artifact_id) return "";
  try {
    return artifactDownloadUrl(artifact.artifact_id);
  } catch {
    // The backend-origin fence is intentionally fail-closed. The preview modal
    // will explain the unavailable route when the user opens it.
    return "";
  }
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function processHtml(trace: ProcessTrace): string {
  const steps = trace.steps
    .map(
      (step, index) => `
        <li class="step ${escapeHtml(step.state)}" style="--i:${index}">
          <span class="dot"></span>
          <div><strong>${escapeHtml(step.label)}</strong>${
            step.detail ? `<small>${escapeHtml(step.detail)}</small>` : ""
          }</div>
        </li>`,
    )
    .join("");
  const artifact = trace.artifact
    ? `<section class="artifact"><div class="artifact-kicker">LIVE ARTIFACT</div><h2>${escapeHtml(
        trace.artifact.title || "动态 HTML 产物",
      )}</h2><p>${escapeHtml(trace.artifact.artifact_type.toUpperCase())} · ${escapeHtml(
        formatSize(trace.artifact.size_bytes),
      )}</p></section>`
    : "";
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(trace.title)}</title>
<style>
:root{font-family:"Noto Sans CJK SC","PingFang SC",sans-serif;color:#152321;background:#f3f1eb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:38px;background:radial-gradient(circle at 85% 12%,#d7f3ea 0,transparent 34%),#f3f1eb}
.shell{max-width:900px;margin:auto;border:1px solid #cfd8d2;border-radius:24px;background:#fbfbf8;box-shadow:0 24px 70px #21453a18;overflow:hidden}
header{padding:28px 30px 22px;border-bottom:1px solid #dfe5e0;display:flex;justify-content:space-between;gap:24px;align-items:flex-start}
.eyebrow{letter-spacing:.16em;font-size:11px;color:#0c806e;font-weight:700}.title{font-size:27px;line-height:1.2;margin:7px 0 0}.status{padding:7px 11px;border-radius:999px;background:#e8f4ef;color:#0c806e;font-size:12px;font-weight:700}
.meta{display:flex;flex-wrap:wrap;gap:8px;padding:18px 30px;background:#f6f7f3}.chip{padding:8px 11px;border:1px solid #d9e1db;border-radius:10px;font-size:12px;color:#47605a;background:#fff}.chip b{color:#152321}
main{padding:25px 30px}.rail{margin:0;padding:0;list-style:none}.step{display:flex;gap:13px;align-items:flex-start;position:relative;padding:0 0 19px;animation:rise .5s both;animation-delay:calc(var(--i)*.09s)}.step:not(:last-child):after{content:"";position:absolute;left:5px;top:14px;bottom:0;width:1px;background:#d4dfd8}.dot{width:11px;height:11px;border-radius:50%;margin-top:3px;background:#b7c5be;box-shadow:0 0 0 4px #edf2ef;z-index:1}.step.done .dot{background:#0c806e}.step.running .dot{background:#d8902d;animation:pulse 1.1s infinite}.step.failed .dot{background:#cf4b46}.step strong{display:block;font-size:14px}.step small{display:block;margin-top:4px;color:#71827c;font-size:12px;line-height:1.5}.artifact{margin-top:9px;padding:20px;border-radius:17px;background:#122d28;color:#f6fffb;position:relative;overflow:hidden}.artifact:after{content:"";position:absolute;width:170px;height:170px;right:-50px;top:-70px;border:1px solid #9ee8d550;border-radius:50%;box-shadow:0 0 0 22px #9ee8d51a,0 0 0 44px #9ee8d511}.artifact-kicker{font-size:10px;letter-spacing:.18em;color:#9ee8d5}.artifact h2{margin:8px 0 4px;font-size:20px}.artifact p{margin:0;color:#b8d6ce;font-size:12px}
footer{padding:16px 30px;border-top:1px solid #dfe5e0;color:#70817b;font-size:11px;display:flex;justify-content:space-between}.pulse-line{width:130px;height:4px;border-radius:99px;background:linear-gradient(90deg,#0c806e,#9ee8d5,#0c806e);background-size:200% 100%;animation:flow 1.6s linear infinite}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}@keyframes pulse{50%{box-shadow:0 0 0 7px #d8902d22}}@keyframes flow{to{background-position:-200% 0}}
</style></head><body><div class="shell"><header><div><div class="eyebrow">ECHODESK PROCESS TRACE</div><div class="title">${escapeHtml(
    trace.title,
  )}</div></div><div class="status">${escapeHtml(statusLabel(trace.status))}</div></header>
<div class="meta"><div class="chip">模型 <b>${escapeHtml(
    trace.model ? modelDisplayName(trace.model) : "未确认",
  )}</b></div><div class="chip">路径 <b>${escapeHtml(trace.route)}</b></div></div>
<main><ol class="rail">${steps}</ol>${artifact}</main><footer><span>动态 HTML 视图 · 数据来自当前可见事件</span><span class="pulse-line"></span></footer></div></body></html>`;
}

function ArtifactActions({ artifact }: { artifact: GeneratedArtifact }): JSX.Element {
  const downloadUrl = safeArtifactUrl(artifact);
  return (
    <div className="echodesk-process-artifact-actions">
      {downloadUrl && (
        <AuthenticatedDownloadLink
          url={downloadUrl}
          downloadName={artifact.title || undefined}
          className="echodesk-process-action"
          testId="process-artifact-download"
        >
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          下载
        </AuthenticatedDownloadLink>
      )}
    </div>
  );
}

export default function ExecutionTraceCard({ trace }: ExecutionTraceCardProps): JSX.Element {
  const [previewArtifact, setPreviewArtifact] = useState<GeneratedArtifact | null>(null);
  const [processHtmlOpen, setProcessHtmlOpen] = useState(false);
  const html = useMemo(() => processHtml(trace), [trace]);
  const artifact = trace.artifact ?? null;
  const isHtmlArtifact = artifact?.artifact_type.toLowerCase() === "html";
  const model = trace.model ? modelDisplayName(trace.model) : "未确认";
  const modelNote =
    trace.modelEvidence === "observed"
      ? "实际回执"
      : trace.modelEvidence === "requested"
        ? "请求配置"
        : "等待回执";
  const TraceIcon = trace.kind === "artifact" ? Globe2 : Sparkles;

  return (
    <article
      className={`echodesk-process-card is-${trace.status}`}
      data-testid="execution-trace-card"
      data-trace-id={trace.id}
      data-trace-status={trace.status}
    >
      <header className="echodesk-process-header">
        <div className="echodesk-process-title-group">
          <span className="echodesk-process-icon" aria-hidden="true">
            <TraceIcon className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="echodesk-process-eyebrow">过程展示</div>
            <h3>{trace.title}</h3>
          </div>
        </div>
        <span className="echodesk-process-status">{statusLabel(trace.status)}</span>
      </header>

      <div className="echodesk-process-meta" data-testid="process-model">
        <span className="echodesk-process-model-chip">
          <Code2 className="h-3.5 w-3.5" aria-hidden="true" />
          <span className="echodesk-process-model-label">模型</span>
          <strong title={model}>{model}</strong>
          <em>{modelNote}</em>
        </span>
        <span className="echodesk-process-route" title={trace.route}>
          <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
          {trace.route}
        </span>
      </div>

      <ol className="echodesk-process-steps" aria-label="处理步骤">
        {trace.steps.map((step) => (
          <li key={step.id} className={`echodesk-process-step is-${step.state}`}>
            {stepIcon(step)}
            <div className="echodesk-process-step-copy">
              <span>{step.label}</span>
              {step.detail && <small title={step.detail}>{step.detail}</small>}
            </div>
          </li>
        ))}
      </ol>

      {trace.error && (
        <div className="echodesk-process-error" role="status">
          <XCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{trace.error}</span>
        </div>
      )}

      {artifact && (
        <div className="echodesk-process-artifact" data-testid="process-artifact-card">
          <div className="echodesk-process-artifact-icon" aria-hidden="true">
            {isHtmlArtifact ? <Globe2 className="h-4 w-4" /> : <FileCode2 className="h-4 w-4" />}
          </div>
          <div className="echodesk-process-artifact-copy">
            <strong title={artifact.title || artifact.artifact_id}>
              {artifact.title || artifact.artifact_id}
            </strong>
            <span>
              {artifact.artifact_type.toUpperCase()} · {formatSize(artifact.size_bytes)}
              {artifact.generation_latency_ms > 0
                ? ` · ${(artifact.generation_latency_ms / 1000).toFixed(1)} 秒`
                : ""}
            </span>
          </div>
          <div className="echodesk-process-artifact-buttons">
            {isHtmlArtifact && (
              <button
                type="button"
                className="echodesk-process-action is-primary"
                onClick={() => setPreviewArtifact(artifact)}
                data-testid="process-html-preview"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                动态预览
              </button>
            )}
            <ArtifactActions artifact={artifact} />
          </div>
        </div>
      )}

      <div className="echodesk-process-footer">
        <span>可回放的事件轨迹</span>
        <span className="inline-flex items-center gap-1.5">
          <button
            type="button"
            className="echodesk-process-html-button"
            onClick={() => setProcessHtmlOpen(true)}
            data-testid="process-html-view"
          >
            <Globe2 className="h-3.5 w-3.5" aria-hidden="true" />
            过程 HTML
          </button>
          <a
            href="./echodesk-process-demo.html"
            target="_blank"
            rel="noreferrer"
            className="echodesk-process-html-button"
            data-testid="process-standalone-html"
          >
            独立演示
          </a>
        </span>
      </div>

      <ArtifactPreviewModal
        artifact={previewArtifact}
        onClose={() => setPreviewArtifact(null)}
      />
      <Modal
        open={processHtmlOpen}
        onCancel={() => setProcessHtmlOpen(false)}
        footer={null}
        width="min(960px, 92vw)"
        title="过程 HTML · EchoDesk"
        destroyOnHidden
      >
        <iframe
          title="EchoDesk 过程 HTML"
          srcDoc={html}
          sandbox=""
          className="echodesk-process-html-frame"
          data-testid="process-html-frame"
        />
      </Modal>
    </article>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { Modal, message } from "antd";
import { Download, ExternalLink, Loader2, AlertCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  artifactDownloadUrl,
  artifactIdFromDownloadHref,
} from "@/api";
import AuthenticatedDownloadLink from "@/components/AuthenticatedDownloadLink";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { apiTransport } from "@/session";
import type { GeneratedArtifact } from "@/types";

/**
 * 7 类产物 in-app 预览：
 *   html / pdf  → authenticated bounded fetch → blob URL → <iframe>
 *   markdown    → fetch text + react-markdown 渲染（GFM 表格 / 代码块）
 *   txt         → fetch text + <pre> 等宽字体
 *   docx (word) → fetch ArrayBuffer + mammoth.convertToHtml → iframe srcDoc
 *   xlsx        → fetch ArrayBuffer + ExcelJS（dynamic import）+ table 渲染
 *   pptx        → 不在 Modal 内预览；调 window.echo.openArtifactInSystem
 *                 让 macOS Keynote 打开（浏览器无法原生渲染 pptx 二进制）
 *
 * 设计权衡：
 *   1. ExcelJS 用 dynamic import 仅在打开 xlsx 时加载，
 *      避免主 bundle 变重；mammoth 同理（更大，3.7MB unpacked）。
 *   2. docx 解析转 HTML 后用 iframe srcDoc 隔离 mammoth 输出的样式，
 *      避免污染 app 自身的 Tailwind / Antd CSS。
 *   3. error path：任何解析失败都给一个明确兜底（红色提示 + 直接下载链接），
 *      不静默丢错；遵循「失败路径必须可观察」原则（19-quality-detail）。
 */

interface ArtifactPreviewModalProps {
  artifact: GeneratedArtifact | null;
  onClose: () => void;
}

interface PreviewOriginLease {
  generation: number;
  isCurrent: (generation: number) => boolean;
  registerAbortController: (controller: AbortController) => () => void;
}

type PreviewKind = "html" | "pdf" | "markdown" | "txt" | "docx" | "xlsx" | "pptx" | "other";

const ARTIFACT_DOWNLOAD_MAX_BYTES = 128 * 1024 * 1024;
const ARTIFACT_TEXT_PREVIEW_MAX_BYTES = 4 * 1024 * 1024;
const ARTIFACT_BINARY_PREVIEW_MAX_BYTES = 32 * 1024 * 1024;
const PPTX_DOWNLOAD_OBJECT_URL_GRACE_MS = 30_000;
const activePptxDownloadUrls = new Map<string, number>();
let pptxUnloadCleanupInstalled = false;

function revokePptxDownloadUrl(objectUrl: string): void {
  const timer = activePptxDownloadUrls.get(objectUrl);
  if (timer !== undefined) window.clearTimeout(timer);
  activePptxDownloadUrls.delete(objectUrl);
  URL.revokeObjectURL(objectUrl);
}

function revokeAllPptxDownloadUrls(): void {
  for (const objectUrl of [...activePptxDownloadUrls.keys()]) {
    revokePptxDownloadUrl(objectUrl);
  }
}

function retainPptxDownloadUrl(objectUrl: string): void {
  if (!pptxUnloadCleanupInstalled) {
    window.addEventListener("pagehide", revokeAllPptxDownloadUrls);
    window.addEventListener("beforeunload", revokeAllPptxDownloadUrls);
    pptxUnloadCleanupInstalled = true;
  }
  const timer = window.setTimeout(
    () => revokePptxDownloadUrl(objectUrl),
    PPTX_DOWNLOAD_OBJECT_URL_GRACE_MS,
  );
  activePptxDownloadUrls.set(objectUrl, timer);
}

const previewTypeLabel: Record<PreviewKind, string> = {
  html: "网页",
  pdf: "PDF",
  markdown: "Markdown",
  txt: "文本",
  docx: "文档",
  xlsx: "表格",
  pptx: "演示文稿",
  other: "文件",
};

function previewTitle(artifact: GeneratedArtifact, kind: PreviewKind): string {
  return artifact.title?.trim() || `未命名${previewTypeLabel[kind]}`;
}

function safeDownloadStem(raw: string): string {
  const withoutControls = Array.from(raw, (character) =>
    character.charCodeAt(0) < 32 ? " " : character,
  ).join("");
  return withoutControls.replace(/[<>:"/\\|?*]+/g, " ").trim();
}

function classifyKind(rawType: string): PreviewKind {
  const t = (rawType || "").toLowerCase();
  if (t === "html") return "html";
  if (t === "pdf") return "pdf";
  if (t === "markdown" || t === "md" || t === "mdown") return "markdown";
  if (t === "txt" || t === "text") return "txt";
  if (t === "word" || t === "docx" || t === "doc") return "docx";
  if (t === "xlsx" || t === "excel" || t === "xls") return "xlsx";
  if (t === "pptx" || t === "ppt") return "pptx";
  return "other";
}

export default function ArtifactPreviewModal({
  artifact,
  onClose,
}: ArtifactPreviewModalProps): JSX.Element {
  const {
    revision: backendOriginRevision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const handledOriginRevision = useRef(backendOriginRevision);
  const pptxOpenAttempt = useRef<GeneratedArtifact | null>(null);
  const onCloseRef = useRef(onClose);
  const kind = useMemo<PreviewKind>(
    () => (artifact ? classifyKind(artifact.artifact_type) : "other"),
    [artifact],
  );
  const artifactOriginGeneration = useMemo(
    () => (artifact ? captureGeneration() : null),
    [artifact, captureGeneration],
  );
  const previewLease = useMemo<PreviewOriginLease | null>(
    () =>
      artifactOriginGeneration === null
        ? null
        : {
            generation: artifactOriginGeneration,
            isCurrent,
            registerAbortController,
          },
    [artifactOriginGeneration, isCurrent, registerAbortController],
  );
  const artifactOriginCurrent =
    artifactOriginGeneration !== null && isCurrent(artifactOriginGeneration);
  const downloadUrl =
    artifact && artifactOriginCurrent
      ? artifactDownloadUrl(artifact.artifact_id)
      : "";

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (handledOriginRevision.current === backendOriginRevision) return;
    handledOriginRevision.current = backendOriginRevision;
    onClose();
  }, [backendOriginRevision, onClose]);

  // pptx 走系统应用打开，Modal 不真展开渲染（artifact 仍非 null 但 open=false 触发 useEffect）
  // 实现：检测到 pptx 时立即触发 openInSystem 并 onClose
  useEffect(() => {
    if (!artifact) {
      pptxOpenAttempt.current = null;
      return;
    }
    if (
      kind !== "pptx" ||
      !previewLease ||
      !previewLease.isCurrent(previewLease.generation) ||
      pptxOpenAttempt.current === artifact
    ) {
      return;
    }
    pptxOpenAttempt.current = artifact;
    let alive = true;
    const controller = new AbortController();
    const unregisterController = previewLease.registerAbortController(controller);
    let pendingObjectUrl: string | null = null;
    let objectUrlRetained = false;
    const canCommit = (): boolean =>
      alive &&
      !controller.signal.aborted &&
      previewLease.isCurrent(previewLease.generation);
    void (async () => {
      try {
        if (!canCommit()) return;
        const bridge = typeof window !== "undefined" ? window.echo : undefined;
        if (
          bridge?.openArtifactInSystem &&
          bridge.isPublicDemo !== true &&
          typeof artifact.file_path === "string" &&
          artifact.file_path.length > 0
        ) {
          await bridge.openArtifactInSystem(artifact.file_path);
          if (canCommit()) {
            void message.success(
              `已用系统应用打开 ${previewTitle(artifact, kind)}`,
            );
          }
        } else {
          // public/remote file_path 没有本机语义。通过同源、身份绑定的 API
          // transport 拉取 Blob，绝不把远端路径交给 Electron main。
          if (!canCommit()) return;
          const response = await apiTransport(
            downloadUrl,
            { signal: controller.signal },
            {
              timeoutMs: 120_000,
              maxResponseBytes: ARTIFACT_DOWNLOAD_MAX_BYTES,
              throwHttpErrors: false,
            },
          );
          if (!response.ok) {
            await response.body?.cancel().catch(() => undefined);
            throw new Error(`下载失败：HTTP ${response.status}`);
          }
          const blob = await response.blob();
          if (!canCommit()) return;
          const objectUrl = URL.createObjectURL(blob);
          pendingObjectUrl = objectUrl;
          const a = document.createElement("a");
          a.href = objectUrl;
          const safeTitle =
            safeDownloadStem(
              artifact.title?.trim() || "echodesk-presentation",
            ) || "echodesk-presentation";
          a.download = `${safeTitle}.pptx`;
          a.rel = "noreferrer";
          document.body.appendChild(a);
          try {
            a.click();
          } finally {
            a.remove();
          }
          retainPptxDownloadUrl(objectUrl);
          objectUrlRetained = true;
          void message.info("浏览器无法预览 PPT，已安全下载到本机");
        }
      } catch (e) {
        if (!canCommit()) return;
        console.error("[artifact-preview] open presentation failed", e);
        void message.error("暂时无法打开演示文稿，请下载后重试");
      } finally {
        if (pendingObjectUrl && !objectUrlRetained) {
          URL.revokeObjectURL(pendingObjectUrl);
        }
        if (canCommit()) onCloseRef.current();
      }
    })();
    return () => {
      alive = false;
      controller.abort();
      unregisterController();
    };
  }, [artifact, kind, downloadUrl, previewLease]);

  const open = !!artifact && artifactOriginCurrent && kind !== "pptx";

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="86%"
      destroyOnHidden
      title={
        artifact ? (
          <div className="flex items-center justify-between gap-3 pr-8">
            <div className="flex flex-col min-w-0">
              <span
                className="text-[14px] text-ink-800 font-semibold truncate"
                title={previewTitle(artifact, kind)}
              >
                {previewTitle(artifact, kind)}
              </span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <AuthenticatedDownloadLink
                url={downloadUrl}
                downloadName={`${safeDownloadStem(previewTitle(artifact, kind)) || "echodesk-artifact"}.${artifact.artifact_type}`}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[12px] text-ink-700 border border-paper-300 hover:bg-paper-150"
                testId="preview-download-btn"
              >
                <Download className="w-3.5 h-3.5" />
                <span>下载</span>
              </AuthenticatedDownloadLink>
              {(kind === "docx" || kind === "xlsx") && previewLease && (
                <OpenInSystemButton artifact={artifact} lease={previewLease} />
              )}
            </div>
          </div>
        ) : null
      }
    >
      {artifact && open && previewLease && (
        <div className="bg-white rounded-md min-h-[60vh]" data-testid="preview-body">
          <PreviewBody
            kind={kind}
            downloadUrl={downloadUrl}
            lease={previewLease}
          />
        </div>
      )}
    </Modal>
  );
}

// ---------- 子组件 ----------

function OpenInSystemButton({
  artifact,
  lease,
}: {
  artifact: GeneratedArtifact;
  lease: PreviewOriginLease;
}): JSX.Element | null {
  // 非 Electron 环境直接不显示，避免给用户假按钮（点了什么也没发生）
  const localFilePath = artifact.file_path;
  const hasBridge =
    typeof window !== "undefined" &&
    window.echo?.isPublicDemo !== true &&
    !!window.echo?.openArtifactInSystem &&
    typeof localFilePath === "string" &&
    localFilePath.length > 0;
  if (!hasBridge) return null;
  return (
    <button
      type="button"
      data-testid="preview-open-in-system-btn"
      onClick={async () => {
        if (!lease.isCurrent(lease.generation)) return;
        try {
          await window.echo!.openArtifactInSystem!(localFilePath);
          if (lease.isCurrent(lease.generation)) {
            void message.success("已用系统应用打开");
          }
        } catch (e) {
          if (!lease.isCurrent(lease.generation)) return;
          console.error("[artifact-preview] open in system failed", e);
          void message.error("暂时无法用系统应用打开，请下载后重试");
        }
      }}
      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[12px] text-ink-700 border border-paper-300 hover:bg-paper-150"
    >
      <ExternalLink className="w-3.5 h-3.5" />
      <span>在系统中打开</span>
    </button>
  );
}

interface PreviewBodyProps {
  kind: PreviewKind;
  downloadUrl: string;
  lease: PreviewOriginLease;
}

function PreviewBody({ kind, downloadUrl, lease }: PreviewBodyProps): JSX.Element {
  switch (kind) {
    case "html":
    case "pdf":
      return <AuthenticatedIframePreview kind={kind} downloadUrl={downloadUrl} lease={lease} />;
    case "markdown":
      return <MarkdownPreview downloadUrl={downloadUrl} lease={lease} />;
    case "txt":
      return <TxtPreview downloadUrl={downloadUrl} lease={lease} />;
    case "docx":
      return <DocxPreview downloadUrl={downloadUrl} lease={lease} />;
    case "xlsx":
      return <XlsxPreview downloadUrl={downloadUrl} lease={lease} />;
    default:
      return (
        <FallbackUnknown
          downloadUrl={downloadUrl}
        />
      );
  }
}

function PreviewLoading(): JSX.Element {
  return (
    <div
      className="flex flex-col items-center justify-center py-20 text-ink-500 text-[12px] gap-2"
      data-testid="preview-loading"
    >
      <Loader2 className="w-5 h-5 animate-spin" aria-hidden="true" />
      <span>正在生成预览…</span>
      <span className="text-ink-400 text-[11px]">
        部分类型（Word / Excel）需在浏览器内解析，可能需要 1-2 秒
      </span>
    </div>
  );
}

function PreviewError({
  message: msg,
  downloadUrl,
}: {
  message: string;
  downloadUrl: string;
}): JSX.Element {
  return (
    <div
      className="mx-auto my-12 max-w-md border border-err/30 bg-err/5 rounded-md px-4 py-3 space-y-2"
      data-testid="preview-error"
    >
      <div className="flex items-start gap-2 text-err text-[13px] font-medium">
        <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
        <span>预览失败</span>
      </div>
      <p className="text-[12px] text-ink-700 leading-relaxed break-all">{msg}</p>
      <AuthenticatedDownloadLink
        url={downloadUrl}
        className="inline-flex items-center gap-1 px-2 py-1 rounded text-[12px] text-accent hover:underline"
      >
        <Download className="w-3.5 h-3.5" />
        <span>直接下载</span>
      </AuthenticatedDownloadLink>
    </div>
  );
}

function AuthenticatedIframePreview({
  kind,
  downloadUrl,
  lease,
}: {
  kind: "html" | "pdf";
  downloadUrl: string;
  lease: PreviewOriginLease;
}): JSX.Element {
  const [state, setState] = useState<{
    loading: boolean;
    objectUrl?: string;
    error?: boolean;
  }>({ loading: true });

  useEffect(() => {
    if (!lease.isCurrent(lease.generation)) return;
    let alive = true;
    let objectUrl: string | null = null;
    const controller = new AbortController();
    const unregisterController = lease.registerAbortController(controller);
    const canCommit = (): boolean =>
      alive && !controller.signal.aborted && lease.isCurrent(lease.generation);
    setState({ loading: true });
    void (async () => {
      try {
        const response = await apiTransport(
          downloadUrl,
          { signal: controller.signal },
          {
            timeoutMs: 60_000,
            maxResponseBytes: ARTIFACT_BINARY_PREVIEW_MAX_BYTES,
            throwHttpErrors: false,
          },
        );
        if (!response.ok) {
          await response.body?.cancel().catch(() => undefined);
          throw new Error(`artifact preview HTTP ${response.status}`);
        }
        const blob = await response.blob();
        if (!canCommit()) return;
        objectUrl = URL.createObjectURL(blob);
        setState({ loading: false, objectUrl });
      } catch {
        if (canCommit()) setState({ loading: false, error: true });
      }
    })();
    return () => {
      alive = false;
      unregisterController();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [downloadUrl, lease]);

  if (state.loading) return <PreviewLoading />;
  if (state.error || !state.objectUrl) {
    return (
      <PreviewError
        message="无法安全加载预览，可下载后用本地应用打开"
        downloadUrl={downloadUrl}
      />
    );
  }
  return (
    <iframe
      src={state.objectUrl}
      title={kind === "html" ? "html preview" : "pdf preview"}
      className="w-full h-[72vh] border border-paper-300 bg-white rounded-md"
      data-testid={`preview-iframe-${kind}`}
      sandbox={kind === "html" ? "" : undefined}
      referrerPolicy="no-referrer"
    />
  );
}

// ---------- 各类型 renderer ----------

function MarkdownPreview({
  downloadUrl,
  lease,
}: {
  downloadUrl: string;
  lease: PreviewOriginLease;
}): JSX.Element {
  const { text, loading, error } = useTextContent(downloadUrl, lease);
  if (loading) return <PreviewLoading />;
  if (error)
    return (
      <PreviewError
        message="无法读取文件内容，可下载后用本地应用打开"
        downloadUrl={downloadUrl}
      />
    );
  return (
    <div
      className="prose prose-sm max-w-none px-6 py-4 h-[72vh] overflow-auto bg-white"
      data-testid="preview-markdown"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => {
            const artifactId = artifactIdFromDownloadHref(href);
            return artifactId ? (
              <AuthenticatedDownloadLink
                url={artifactDownloadUrl(artifactId)}
                className="text-blue-600 underline underline-offset-2"
              >
                {children}
              </AuthenticatedDownloadLink>
            ) : (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function TxtPreview({
  downloadUrl,
  lease,
}: {
  downloadUrl: string;
  lease: PreviewOriginLease;
}): JSX.Element {
  const { text, loading, error } = useTextContent(downloadUrl, lease);
  if (loading) return <PreviewLoading />;
  if (error)
    return (
      <PreviewError
        message="无法读取文件内容，可下载后用本地应用打开"
        downloadUrl={downloadUrl}
      />
    );
  return (
    <pre
      className="px-6 py-4 h-[72vh] overflow-auto bg-paper-50 text-[12px] leading-relaxed font-mono text-ink-800 whitespace-pre-wrap break-words"
      data-testid="preview-txt"
    >
      {text}
    </pre>
  );
}

function DocxPreview({
  downloadUrl,
  lease,
}: {
  downloadUrl: string;
  lease: PreviewOriginLease;
}): JSX.Element {
  const [state, setState] = useState<{
    loading: boolean;
    html?: string;
    error?: string;
  }>({ loading: true });

  useEffect(() => {
    if (!lease.isCurrent(lease.generation)) return;
    let alive = true;
    const controller = new AbortController();
    const unregisterController = lease.registerAbortController(controller);
    const canCommit = (): boolean =>
      alive &&
      lease.isCurrent(lease.generation) &&
      !controller.signal.aborted;
    setState({ loading: true });
    void (async () => {
      try {
        const buf = await fetchArrayBuffer(downloadUrl, controller.signal);
        if (!canCommit()) return;
        // mammoth.browser.js 是 UMD 包；Vite 走 CJS interop 后顶层是 default。
        // 用 unknown + 双重 fallback 保证 ESM / CJS 两边都能 resolve convertToHtml。
        const mod = (await import("mammoth/mammoth.browser.js")) as unknown as {
          convertToHtml?: (opts: { arrayBuffer: ArrayBuffer }) => Promise<{ value: string }>;
          default?: {
            convertToHtml?: (opts: { arrayBuffer: ArrayBuffer }) => Promise<{ value: string }>;
          };
        };
        const fn = mod.convertToHtml ?? mod.default?.convertToHtml;
        if (!fn) throw new Error("mammoth.convertToHtml not found");
        if (!canCommit()) return;
        const result = await fn({ arrayBuffer: buf });
        if (canCommit()) setState({ loading: false, html: result.value });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (canCommit()) setState({ loading: false, error: msg });
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [downloadUrl, lease]);

  if (state.loading) return <PreviewLoading />;
  if (state.error)
    return (
      <PreviewError
        message="无法生成文档预览，可下载后用 Word 或兼容应用打开"
        downloadUrl={downloadUrl}
      />
    );
  // srcDoc 给 iframe 一个独立 DOM，避免 mammoth 内联 style 污染主应用 CSS
  const wrapped = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; padding: 24px; color: #1f1f1f; line-height: 1.6; max-width: 860px; margin: 0 auto; }
    h1, h2, h3 { color: #0f172a; }
    table { border-collapse: collapse; margin: 12px 0; }
    td, th { border: 1px solid #d4d4d8; padding: 6px 10px; }
    img { max-width: 100%; height: auto; }
  </style></head><body>${state.html ?? ""}</body></html>`;
  return (
    <iframe
      srcDoc={wrapped}
      title="docx preview"
      className="w-full h-[72vh] border border-paper-300 bg-white rounded-md"
      data-testid="preview-docx"
      sandbox="allow-same-origin"
    />
  );
}

interface XlsxSheet {
  name: string;
  rows: string[][];
}

const XLSX_MAX_FILE_BYTES = 10 * 1024 * 1024;
const XLSX_MAX_SHEETS = 20;
const XLSX_MAX_ROWS_PER_SHEET = 1_000;
const XLSX_MAX_COLUMNS_PER_SHEET = 100;
const XLSX_MAX_RENDERED_CELLS = 20_000;
const XLSX_PARSE_TIMEOUT_MS = 10_000;

class XlsxPreviewError extends Error {}

async function parseXlsxSheets(buf: ArrayBuffer): Promise<XlsxSheet[]> {
  if (buf.byteLength > XLSX_MAX_FILE_BYTES) {
    throw new XlsxPreviewError("文件超过 10 MiB，已停止在线预览");
  }

  const { default: ExcelJS } = await import("exceljs");
  const workbook = new ExcelJS.Workbook();
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      workbook.xlsx.load(buf),
      new Promise<never>((_, reject) => {
        timeoutId = setTimeout(
          () => reject(new XlsxPreviewError("表格解析超过 10 秒，已停止在线预览")),
          XLSX_PARSE_TIMEOUT_MS,
        );
      }),
    ]);
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }

  if (workbook.worksheets.length > XLSX_MAX_SHEETS) {
    throw new XlsxPreviewError("工作表超过 20 个，已停止在线预览");
  }

  let renderedCells = 0;
  return workbook.worksheets.map((worksheet) => {
    if (worksheet.rowCount > XLSX_MAX_ROWS_PER_SHEET) {
      throw new XlsxPreviewError("单个工作表超过 1000 行，已停止在线预览");
    }
    if (worksheet.columnCount > XLSX_MAX_COLUMNS_PER_SHEET) {
      throw new XlsxPreviewError("单个工作表超过 100 列，已停止在线预览");
    }

    const rows: string[][] = [];
    worksheet.eachRow({ includeEmpty: false }, (row) => {
      const width = row.cellCount;
      renderedCells += width;
      if (renderedCells > XLSX_MAX_RENDERED_CELLS) {
        throw new XlsxPreviewError("单元格超过 20000 个，已停止在线预览");
      }
      rows.push(Array.from({ length: width }, (_, index) => row.getCell(index + 1).text));
    });
    return { name: worksheet.name, rows };
  });
}

function XlsxPreview({
  downloadUrl,
  lease,
}: {
  downloadUrl: string;
  lease: PreviewOriginLease;
}): JSX.Element {
  const [state, setState] = useState<{
    loading: boolean;
    sheets?: XlsxSheet[];
    error?: string;
  }>({ loading: true });
  const [activeIdx, setActiveIdx] = useState(0);

  useEffect(() => {
    if (!lease.isCurrent(lease.generation)) return;
    let alive = true;
    const controller = new AbortController();
    const unregisterController = lease.registerAbortController(controller);
    const canCommit = (): boolean =>
      alive &&
      lease.isCurrent(lease.generation) &&
      !controller.signal.aborted;
    setState({ loading: true });
    setActiveIdx(0);
    void (async () => {
      try {
        const buf = await fetchArrayBuffer(downloadUrl, controller.signal);
        if (!canCommit()) return;
        const sheets = await parseXlsxSheets(buf);
        if (canCommit()) setState({ loading: false, sheets });
      } catch (e) {
        const msg =
          e instanceof XlsxPreviewError
            ? e.message
            : "无法生成表格预览，可下载后用 Excel 或兼容应用打开";
        if (canCommit()) setState({ loading: false, error: msg });
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [downloadUrl, lease]);

  if (state.loading) return <PreviewLoading />;
  if (state.error)
    return (
      <PreviewError
        message={state.error}
        downloadUrl={downloadUrl}
      />
    );
  const sheets = state.sheets ?? [];
  if (sheets.length === 0)
    return (
      <PreviewError message="工作簿没有任何 sheet" downloadUrl={downloadUrl} />
    );
  const safeIdx = Math.min(activeIdx, sheets.length - 1);
  return (
    <div className="flex flex-col h-[72vh]" data-testid="preview-xlsx">
      {sheets.length > 1 && (
        <div className="flex items-center gap-1 px-3 py-1.5 border-b border-paper-300 bg-paper-50 overflow-x-auto shrink-0">
          {sheets.map((s, i) => (
            <button
              key={s.name}
              type="button"
              data-testid={`preview-xlsx-tab-${i}`}
              onClick={() => setActiveIdx(i)}
              className={`px-2.5 py-1 text-[12px] rounded-md transition-colors ${
                i === safeIdx
                  ? "bg-white text-ink-800 border border-paper-300"
                  : "text-ink-600 hover:bg-paper-150"
              }`}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <div className="flex-1 overflow-auto px-3 py-3 bg-white text-[12px]">
        {sheets[safeIdx].rows.length === 0 ? (
          <p className="text-ink-500">当前工作表没有数据</p>
        ) : (
          <table className="border-collapse">
            <tbody>
              {sheets[safeIdx].rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, columnIndex) => (
                    <td
                      key={columnIndex}
                      className="border border-paper-300 px-2 py-1 whitespace-pre-wrap"
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function FallbackUnknown({
  downloadUrl,
}: {
  downloadUrl: string;
}): JSX.Element {
  return (
    <PreviewError
      message="暂不支持在应用内预览此类文件，请下载后用本地应用打开"
      downloadUrl={downloadUrl}
    />
  );
}

// ---------- hooks ----------

function useTextContent(
  downloadUrl: string,
  lease: PreviewOriginLease,
): {
  text: string;
  loading: boolean;
  error?: string;
} {
  const [state, setState] = useState<{
    text: string;
    loading: boolean;
    error?: string;
  }>({ text: "", loading: true });

  useEffect(() => {
    if (!lease.isCurrent(lease.generation)) return;
    let alive = true;
    const controller = new AbortController();
    const unregisterController = lease.registerAbortController(controller);
    const canCommit = (): boolean =>
      alive &&
      lease.isCurrent(lease.generation) &&
      !controller.signal.aborted;
    setState({ text: "", loading: true });
    void (async () => {
      try {
        const resp = await apiTransport(
          downloadUrl,
          { signal: controller.signal },
          {
            timeoutMs: 30_000,
            maxResponseBytes: ARTIFACT_TEXT_PREVIEW_MAX_BYTES,
            throwHttpErrors: false,
          },
        );
        if (!resp.ok) {
          throw new Error(`下载失败：HTTP ${resp.status}`);
        }
        const text = await resp.text();
        if (canCommit()) setState({ text, loading: false });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (canCommit()) setState({ text: "", loading: false, error: msg });
      }
    })();
    return () => {
      alive = false;
      unregisterController();
    };
  }, [downloadUrl, lease]);

  return state;
}

async function fetchArrayBuffer(
  url: string,
  signal: AbortSignal,
): Promise<ArrayBuffer> {
  const resp = await apiTransport(
    url,
    { signal },
    {
      timeoutMs: 30_000,
      maxResponseBytes: ARTIFACT_BINARY_PREVIEW_MAX_BYTES,
      throwHttpErrors: false,
    },
  );
  if (!resp.ok) {
    throw new Error(`下载失败：HTTP ${resp.status}`);
  }
  return resp.arrayBuffer();
}

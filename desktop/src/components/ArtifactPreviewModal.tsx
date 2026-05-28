import { useEffect, useMemo, useState } from "react";
import { Modal, message } from "antd";
import { Download, ExternalLink, Loader2, AlertCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { artifactDownloadUrl } from "@/api";
import type { GeneratedArtifact } from "@/types";

/**
 * 7 类产物 in-app 预览：
 *   html        → fetch text + sandboxed iframe srcDoc（避免 download attachment 在 iframe 空白）
 *   pdf         → <iframe> 直读 download URL（浏览器原生支持）
 *   markdown    → fetch text + react-markdown 渲染（GFM 表格 / 代码块）
 *   txt         → fetch text + <pre> 等宽字体
 *   docx (word) → fetch ArrayBuffer + mammoth.convertToHtml → iframe srcDoc
 *   xlsx        → fetch ArrayBuffer + SheetJS（dynamic import）+ table 渲染
 *   pptx        → 不在 Modal 内预览；调 window.echo.openArtifactInSystem
 *                 让 macOS Keynote 打开（浏览器无法原生渲染 pptx 二进制）
 *
 * 设计权衡：
 *   1. xlsx 包 ~600KB gzip，用 dynamic import 仅在打开 xlsx 时加载，
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

type PreviewKind = "html" | "pdf" | "markdown" | "txt" | "docx" | "xlsx" | "pptx" | "other";

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
  const kind = useMemo<PreviewKind>(
    () => (artifact ? classifyKind(artifact.artifact_type) : "other"),
    [artifact],
  );
  const downloadUrl = artifact ? artifactDownloadUrl(artifact.artifact_id) : "";

  // pptx 走系统应用打开，Modal 不真展开渲染（artifact 仍非 null 但 open=false 触发 useEffect）
  // 实现：检测到 pptx 时立即触发 openInSystem 并 onClose
  useEffect(() => {
    if (!artifact || kind !== "pptx") return;
    void (async () => {
      try {
        const bridge = typeof window !== "undefined" ? window.echo : undefined;
        if (bridge?.openArtifactInSystem) {
          await bridge.openArtifactInSystem(artifact.file_path);
          void message.success(
            `已用系统应用打开 ${artifact.title || artifact.artifact_id}`,
          );
        } else {
          // 非 Electron 环境（浏览器 dev / e2e fallback）：退化为提示用户下载
          void message.info("浏览器无法预览 PPT，已为你触发下载");
          const a = document.createElement("a");
          a.href = downloadUrl;
          a.download = "";
          a.rel = "noreferrer";
          document.body.appendChild(a);
          a.click();
          a.remove();
        }
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        void message.error(`打开 PPT 失败：${msg}`);
      } finally {
        onClose();
      }
    })();
  }, [artifact, kind, downloadUrl, onClose]);

  const open = !!artifact && kind !== "pptx";

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="86%"
      destroyOnClose
      title={
        artifact ? (
          <div className="flex items-center justify-between gap-3 pr-8">
            <div className="flex flex-col min-w-0">
              <span
                className="text-[14px] text-ink-800 font-semibold truncate"
                title={artifact.title || artifact.artifact_id}
              >
                {artifact.title || artifact.artifact_id}
              </span>
              {artifact.title && (
                <span
                  className="font-mono text-[10px] text-ink-400 truncate"
                  title={artifact.artifact_id}
                >
                  {artifact.artifact_id}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <a
                href={downloadUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[12px] text-ink-700 border border-paper-300 hover:bg-paper-150"
                data-testid="preview-download-btn"
              >
                <Download className="w-3.5 h-3.5" />
                <span>下载</span>
              </a>
              {(kind === "docx" || kind === "xlsx") && (
                <OpenInSystemButton artifact={artifact} />
              )}
            </div>
          </div>
        ) : null
      }
    >
      {artifact && open && (
        <div className="bg-white rounded-md min-h-[60vh]" data-testid="preview-body">
          <PreviewBody artifact={artifact} kind={kind} downloadUrl={downloadUrl} />
        </div>
      )}
    </Modal>
  );
}

// ---------- 子组件 ----------

function OpenInSystemButton({
  artifact,
}: {
  artifact: GeneratedArtifact;
}): JSX.Element | null {
  // 非 Electron 环境直接不显示，避免给用户假按钮（点了什么也没发生）
  const hasBridge =
    typeof window !== "undefined" && !!window.echo?.openArtifactInSystem;
  if (!hasBridge) return null;
  return (
    <button
      type="button"
      data-testid="preview-open-in-system-btn"
      onClick={async () => {
        try {
          await window.echo!.openArtifactInSystem!(artifact.file_path);
          void message.success("已用系统应用打开");
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          void message.error(`打开失败：${msg}`);
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
  artifact: GeneratedArtifact;
  kind: PreviewKind;
  downloadUrl: string;
}

function PreviewBody({ artifact, kind, downloadUrl }: PreviewBodyProps): JSX.Element {
  switch (kind) {
    case "html":
      return <HtmlPreview downloadUrl={downloadUrl} />;
    case "pdf":
      return (
        <iframe
          src={downloadUrl}
          title="pdf preview"
          className="w-full h-[72vh] border border-paper-300 bg-white rounded-md"
          data-testid={`preview-iframe-${kind}`}
        />
      );
    case "markdown":
      return <MarkdownPreview downloadUrl={downloadUrl} />;
    case "txt":
      return <TxtPreview downloadUrl={downloadUrl} />;
    case "docx":
      return <DocxPreview downloadUrl={downloadUrl} />;
    case "xlsx":
      return <XlsxPreview downloadUrl={downloadUrl} />;
    default:
      return (
        <FallbackUnknown
          artifactType={artifact.artifact_type}
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
      <a
        href={downloadUrl}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 px-2 py-1 rounded text-[12px] text-accent hover:underline"
      >
        <Download className="w-3.5 h-3.5" />
        <span>直接下载</span>
      </a>
    </div>
  );
}

// ---------- 各类型 renderer ----------

function HtmlPreview({ downloadUrl }: { downloadUrl: string }): JSX.Element {
  const { text, loading, error } = useTextContent(downloadUrl);
  if (loading) return <PreviewLoading />;
  if (error) return <PreviewError message={error} downloadUrl={downloadUrl} />;
  if (!text.trim()) {
    return <PreviewError message="HTML 文件为空，无法预览" downloadUrl={downloadUrl} />;
  }
  return (
    <iframe
      srcDoc={text}
      title="html preview"
      className="w-full h-[72vh] border border-paper-300 bg-white rounded-md"
      data-testid="preview-iframe-html"
      sandbox="allow-scripts allow-forms allow-popups"
      referrerPolicy="no-referrer"
    />
  );
}

function MarkdownPreview({ downloadUrl }: { downloadUrl: string }): JSX.Element {
  const { text, loading, error } = useTextContent(downloadUrl);
  if (loading) return <PreviewLoading />;
  if (error) return <PreviewError message={error} downloadUrl={downloadUrl} />;
  return (
    <div
      className="prose prose-sm max-w-none px-6 py-4 h-[72vh] overflow-auto bg-white"
      data-testid="preview-markdown"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function TxtPreview({ downloadUrl }: { downloadUrl: string }): JSX.Element {
  const { text, loading, error } = useTextContent(downloadUrl);
  if (loading) return <PreviewLoading />;
  if (error) return <PreviewError message={error} downloadUrl={downloadUrl} />;
  return (
    <pre
      className="px-6 py-4 h-[72vh] overflow-auto bg-paper-50 text-[12px] leading-relaxed font-mono text-ink-800 whitespace-pre-wrap break-words"
      data-testid="preview-txt"
    >
      {text}
    </pre>
  );
}

function DocxPreview({ downloadUrl }: { downloadUrl: string }): JSX.Element {
  const [state, setState] = useState<{
    loading: boolean;
    html?: string;
    error?: string;
  }>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const buf = await fetchArrayBuffer(downloadUrl);
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
        const result = await fn({ arrayBuffer: buf });
        if (!cancelled) setState({ loading: false, html: result.value });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (!cancelled) setState({ loading: false, error: msg });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [downloadUrl]);

  if (state.loading) return <PreviewLoading />;
  if (state.error)
    return <PreviewError message={state.error} downloadUrl={downloadUrl} />;
  // srcDoc 给 iframe 一个独立 DOM，避免 mammoth 内联 style 污染主应用 CSS
  const wrapped = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; padding: 24px; color: #1f1f1f; line-height: 1.6; max-width: 860px; margin: 0 auto; }
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
  html: string;
}

function XlsxPreview({ downloadUrl }: { downloadUrl: string }): JSX.Element {
  const [state, setState] = useState<{
    loading: boolean;
    sheets?: XlsxSheet[];
    error?: string;
  }>({ loading: true });
  const [activeIdx, setActiveIdx] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const buf = await fetchArrayBuffer(downloadUrl);
        // xlsx 是 CJS；Vite interop 下 default 拿到所有方法，命名导出也行
        const mod = (await import("xlsx")) as unknown as typeof import("xlsx") & {
          default?: typeof import("xlsx");
        };
        const XLSX = mod.default ?? mod;
        const wb = XLSX.read(buf, { type: "array" });
        const sheets: XlsxSheet[] = wb.SheetNames.map((name) => ({
          name,
          html: XLSX.utils.sheet_to_html(wb.Sheets[name], { editable: false }),
        }));
        if (!cancelled) setState({ loading: false, sheets });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (!cancelled) setState({ loading: false, error: msg });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [downloadUrl]);

  if (state.loading) return <PreviewLoading />;
  if (state.error)
    return <PreviewError message={state.error} downloadUrl={downloadUrl} />;
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
        <div
          className="xlsx-preview-table [&_table]:border-collapse [&_td]:border [&_td]:border-paper-300 [&_th]:border [&_th]:border-paper-300 [&_td]:px-2 [&_td]:py-1 [&_th]:px-2 [&_th]:py-1 [&_th]:bg-paper-100"
          // sheet_to_html 出来是受信任的（由我们自己 backend 生成的产物）；
          // 仍然只在受沙箱化 div 内插入，避免 script 副作用。
          dangerouslySetInnerHTML={{ __html: sheets[safeIdx].html }}
        />
      </div>
    </div>
  );
}

function FallbackUnknown({
  artifactType,
  downloadUrl,
}: {
  artifactType: string;
  downloadUrl: string;
}): JSX.Element {
  return (
    <PreviewError
      message={`暂不支持在应用内预览类型「${artifactType}」，请下载后用本地软件打开`}
      downloadUrl={downloadUrl}
    />
  );
}

// ---------- hooks ----------

function useTextContent(downloadUrl: string): {
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
    let cancelled = false;
    setState({ text: "", loading: true });
    void (async () => {
      try {
        const resp = await fetch(downloadUrl);
        if (!resp.ok) {
          throw new Error(`下载失败：HTTP ${resp.status}`);
        }
        const text = await resp.text();
        if (!cancelled) setState({ text, loading: false });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (!cancelled) setState({ text: "", loading: false, error: msg });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [downloadUrl]);

  return state;
}

async function fetchArrayBuffer(url: string): Promise<ArrayBuffer> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`下载失败：HTTP ${resp.status}`);
  }
  return resp.arrayBuffer();
}

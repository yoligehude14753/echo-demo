import { useState } from "react";
import { Button, Input, Modal, Segmented, message } from "antd";
import {
  Download,
  Sparkles,
  FileText,
  FileSpreadsheet,
  Globe,
  Presentation,
} from "lucide-react";
import { artifactDownloadUrl, generateArtifact } from "@/api";
import type { ArtifactKind } from "@/api";
import { useStore } from "@/store";
import type { GeneratedArtifact } from "@/types";

type GenerateKind = "html" | "pptx" | "xlsx" | "word";

const typeIcon: Record<string, JSX.Element> = {
  word: <FileText className="w-3.5 h-3.5" />,
  xlsx: <FileSpreadsheet className="w-3.5 h-3.5" />,
  excel: <FileSpreadsheet className="w-3.5 h-3.5" />,
  pptx: <Presentation className="w-3.5 h-3.5" />,
  ppt: <Presentation className="w-3.5 h-3.5" />,
  html: <Globe className="w-3.5 h-3.5" />,
};

const typeBadge: Record<string, string> = {
  word: "bg-blue-50 text-blue-700",
  xlsx: "bg-emerald-50 text-emerald-700",
  excel: "bg-emerald-50 text-emerald-700",
  pptx: "bg-amber-50 text-amber-700",
  ppt: "bg-amber-50 text-amber-700",
  html: "bg-violet-50 text-violet-700",
};

export default function ArtifactPanel(): JSX.Element {
  const artifacts = useStore((s) => s.artifacts);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<GenerateKind>("html");
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [previewArtifact, setPreviewArtifact] =
    useState<GeneratedArtifact | null>(null);

  const submit = async (): Promise<void> => {
    if (!brief.trim()) {
      message.warning("请填写生成指令");
      return;
    }
    setBusy(true);
    try {
      const a = await generateArtifact({
        artifact_type: kind as ArtifactKind,
        brief,
      });
      message.success(`已生成 ${a.artifact_type}`);
      setOpen(false);
      setBrief("");
    } catch (e: unknown) {
      message.error(`生成失败：${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col bg-paper-50">
      <div className="flex items-center justify-between px-6 h-11 border-b border-paper-300">
        <span className="text-[13px] text-ink-700 font-medium">产物</span>
        <Button
          type="primary"
          size="small"
          icon={<Sparkles className="w-3.5 h-3.5" />}
          onClick={() => setOpen(true)}
        >
          生成
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
        {artifacts.length === 0 && (
          <div className="px-3 py-8 text-center text-ink-400 text-[11px]">
            点击右上角「生成」开始
          </div>
        )}
        {artifacts.map((a) => (
          <div
            key={a.artifact_id}
            className="group px-3 py-2.5 rounded-lg hover:bg-paper-150 cursor-pointer transition-colors"
            onClick={() =>
              a.artifact_type === "html" ? setPreviewArtifact(a) : undefined
            }
          >
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-[13px] text-ink-800 font-medium truncate">
                <span
                  className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${
                    typeBadge[a.artifact_type] ?? "bg-paper-200 text-ink-700"
                  }`}
                >
                  {typeIcon[a.artifact_type] ?? null}
                  <span className="uppercase">{a.artifact_type}</span>
                </span>
                <span className="font-mono text-[12px] text-ink-600 truncate">
                  {a.artifact_id}
                </span>
              </span>
              <a
                href={artifactDownloadUrl(a.artifact_id)}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="opacity-0 group-hover:opacity-100 transition-opacity"
              >
                <Download className="w-4 h-4 text-ink-500 hover:text-accent" />
              </a>
            </div>
            <div className="text-[11px] text-ink-400 mt-1 flex items-center gap-2 pl-1">
              <span>{(a.size_bytes / 1024).toFixed(1)} KB</span>
              <span>·</span>
              <span>{(a.generation_latency_ms / 1000).toFixed(1)}s</span>
              <span>·</span>
              <span className="font-mono text-[10px]">{a.model}</span>
            </div>
          </div>
        ))}
      </div>

      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        onOk={submit}
        confirmLoading={busy}
        title="生成产物"
        okText="生成"
        cancelText="取消"
      >
        <div className="space-y-3 pt-2">
          <Segmented
            options={[
              { label: "HTML", value: "html" },
              { label: "PPT", value: "pptx" },
              { label: "Excel", value: "xlsx" },
              { label: "Word", value: "word" },
            ]}
            value={kind}
            onChange={(v) => setKind(v as GenerateKind)}
            block
          />
          <Input.TextArea
            rows={5}
            placeholder={
              kind === "pptx"
                ? "例如：生成 12 页英伟达 2025 投资展望 PPT，含数据表+柱图"
                : kind === "xlsx"
                  ? "例如：英伟达过去 5 年营收 + 2026 预测，含 DCF / WACC / 敏感性分析"
                  : kind === "word"
                    ? "例如：生成英伟达投资展望 Word 报告，含目录 / 表格"
                    : "例如：生成英伟达 2020-2025 营收快照 HTML，含 SVG 柱图"
            }
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
          />
        </div>
      </Modal>

      <Modal
        open={!!previewArtifact}
        onCancel={() => setPreviewArtifact(null)}
        footer={null}
        width="86%"
        title={
          <span className="font-mono text-[12px] text-ink-700">
            {previewArtifact?.artifact_id}
          </span>
        }
      >
        {previewArtifact && (
          <iframe
            src={artifactDownloadUrl(previewArtifact.artifact_id)}
            title="preview"
            className="w-full h-[72vh] border border-paper-300 bg-white rounded-md"
          />
        )}
      </Modal>
    </div>
  );
}

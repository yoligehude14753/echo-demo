import { useState } from "react";
import { Modal } from "antd";
import {
  Download,
  FileText,
  FileSpreadsheet,
  Globe,
  Presentation,
} from "lucide-react";
import { artifactDownloadUrl } from "@/api";
import { useStore } from "@/store";
import type { GeneratedArtifact } from "@/types";

/**
 * outputs 面板：展示历史产物列表（只读）。
 *
 * 2026-05 修订：
 * - 重命名「产物」→「outputs」
 * - 删除右上角「生成」按钮 + 生成 Modal：产出由 @ 指令触发（CommandBar）
 * - 这里只展示历史，点击 html 可在 Modal 里预览
 */
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
  const [previewArtifact, setPreviewArtifact] =
    useState<GeneratedArtifact | null>(null);

  return (
    <div className="flex-1 flex flex-col bg-paper-50">
      <div className="flex items-center justify-between px-6 h-11 border-b border-paper-300">
        <span className="text-[13px] text-ink-700 font-medium lowercase tracking-wider">
          outputs
        </span>
        <span className="text-[11px] text-ink-400">{artifacts.length}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
        {artifacts.length === 0 && (
          <div className="px-3 py-8 text-center text-ink-400 text-[11px] space-y-1">
            <div>暂无产物</div>
            <div className="text-ink-300">
              在输入框输入 @生成 PPT / @报告 / @Excel … 触发
            </div>
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

import { useState } from "react";
import { Button, Input, List, Modal, Segmented, Tag, message } from "antd";
import { Download, Sparkles, FileText, FileSpreadsheet, Globe } from "lucide-react";
import { artifactDownloadUrl, generateArtifact } from "@/api";
import { useStore } from "@/store";
import type { GeneratedArtifact } from "@/types";

const typeIcon: Record<string, JSX.Element> = {
  word: <FileText className="w-3.5 h-3.5" />,
  xlsx: <FileSpreadsheet className="w-3.5 h-3.5" />,
  excel: <FileSpreadsheet className="w-3.5 h-3.5" />,
  html: <Globe className="w-3.5 h-3.5" />,
};

export default function ArtifactPanel(): JSX.Element {
  const artifacts = useStore((s) => s.artifacts);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"word" | "xlsx" | "html">("html");
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
      const a = await generateArtifact({ artifact_type: kind, brief });
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
    <div className="flex-1 flex flex-col">
      <div className="flex items-center justify-between px-6 py-3 border-b border-bg-700">
        <span className="text-sm text-slate-300">产物</span>
        <Button
          type="primary"
          size="small"
          icon={<Sparkles className="w-3.5 h-3.5" />}
          onClick={() => setOpen(true)}
        >
          生成
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto px-3 py-2">
        <List
          dataSource={artifacts}
          locale={{
            emptyText: (
              <span className="text-slate-500 text-xs">尚无产物</span>
            ),
          }}
          renderItem={(a) => (
            <List.Item
              key={a.artifact_id}
              className="!px-3 !py-2 hover:bg-bg-700/60 rounded-md cursor-pointer"
              onClick={() =>
                a.artifact_type === "html" ? setPreviewArtifact(a) : undefined
              }
            >
              <div className="w-full">
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 text-sm text-slate-200 truncate">
                    {typeIcon[a.artifact_type] ?? null}
                    <span>{a.artifact_id}</span>
                  </span>
                  <a
                    href={artifactDownloadUrl(a.artifact_id)}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Download className="w-4 h-4 text-slate-400 hover:text-accent" />
                  </a>
                </div>
                <div className="text-xs text-slate-500 mt-1 flex items-center gap-2 flex-wrap">
                  <Tag>{a.artifact_type}</Tag>
                  <span>{(a.size_bytes / 1024).toFixed(1)} KB</span>
                  <span>{(a.generation_latency_ms / 1000).toFixed(1)}s</span>
                  <span className="text-slate-600">{a.model}</span>
                </div>
              </div>
            </List.Item>
          )}
        />
      </div>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        onOk={submit}
        confirmLoading={busy}
        title="生成产物"
        okText="生成"
      >
        <div className="space-y-3">
          <Segmented
            options={[
              { label: "HTML", value: "html" },
              { label: "Excel", value: "xlsx" },
              { label: "Word", value: "word" },
            ]}
            value={kind}
            onChange={(v) => setKind(v as typeof kind)}
            block
          />
          <Input.TextArea
            rows={5}
            placeholder="例如：生成一份英伟达 2020-2025 营收预测 HTML 简报，深色主题，含 SVG 柱图"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
          />
        </div>
      </Modal>
      <Modal
        open={!!previewArtifact}
        onCancel={() => setPreviewArtifact(null)}
        footer={null}
        width="80%"
        title={previewArtifact?.artifact_id}
      >
        {previewArtifact && (
          <iframe
            src={artifactDownloadUrl(previewArtifact.artifact_id)}
            title="preview"
            className="w-full h-[70vh] border-0 bg-white rounded"
          />
        )}
      </Modal>
    </div>
  );
}

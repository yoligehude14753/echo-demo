import type { GeneratedArtifact } from "@/types";

const DOWNLOAD_EXTENSIONS: Record<string, string> = {
  word: "docx",
  docx: "docx",
  excel: "xlsx",
  xlsx: "xlsx",
  ppt: "pptx",
  pptx: "pptx",
  html: "html",
  markdown: "md",
  md: "md",
  pdf: "pdf",
  txt: "txt",
  text: "txt",
};

const MAX_DOWNLOAD_BASENAME_LENGTH = 160;

function safeArtifactBasename(rawTitle: string): string {
  const normalized = rawTitle.normalize("NFKC");
  const basename = normalized.split(/[\\/]/).at(-1) ?? "";
  return basename
    .replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "-")
    .replace(/[. ]+$/g, "")
    .trim();
}

export function artifactDownloadName(
  artifact: Pick<GeneratedArtifact, "artifact_type" | "title">,
): string {
  const extension =
    DOWNLOAD_EXTENSIONS[artifact.artifact_type.toLocaleLowerCase()] ?? "bin";
  const fallback = `echodesk-artifact.${extension}`;
  const basename = safeArtifactBasename(artifact.title?.trim() ?? "");
  if (!basename) return fallback;

  const suffix = `.${extension}`;
  if (basename.toLocaleLowerCase().endsWith(suffix)) {
    return basename.slice(0, MAX_DOWNLOAD_BASENAME_LENGTH);
  }
  const maxStemLength = MAX_DOWNLOAD_BASENAME_LENGTH - suffix.length;
  const stem = basename.slice(0, maxStemLength).replace(/[. ]+$/g, "");
  return `${stem || "echodesk-artifact"}${suffix}`;
}

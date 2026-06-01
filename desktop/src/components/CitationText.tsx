import { Popover } from "antd";
import type { RagCitationReference } from "@/api";

interface CitationTextProps {
  text: string;
  citations?: RagCitationReference[];
  appendUnreferenced?: boolean;
  activeKey: string | null;
  onActiveKeyChange: (key: string | null) => void;
}

type CitationPart =
  | { type: "text"; text: string }
  | { type: "citation"; key: string; number: number; citation: RagCitationReference };

interface CitationRenderModel {
  parts: CitationPart[];
  ordered: Array<{ key: string; number: number; citation: RagCitationReference }>;
}

const DOC_TOKEN_RE = /\[doc:([^\]\s]+)(?:\s+p?(\d+))?[^\]]*\]/g;

function citationKey(citation: RagCitationReference): string {
  return citation.chunk_id ?? citation.url ?? citation.doc_id ?? `${citation.kind}:unknown`;
}

function fallbackCitation(chunkId: string, page?: string): RagCitationReference {
  return {
    kind: "rag",
    chunk_id: chunkId,
    page,
    title: "引用来源",
  };
}

function findCitation(
  tokenChunkId: string,
  citations: RagCitationReference[],
): RagCitationReference | undefined {
  return citations.find((c) => {
    if (c.chunk_id === tokenChunkId) return true;
    if (c.doc_id === tokenChunkId) return true;
    if (c.chunk_id && tokenChunkId.includes(c.chunk_id)) return true;
    return Boolean(c.doc_id && tokenChunkId.startsWith(c.doc_id));
  });
}

function buildCitationModel(
  text: string,
  citations: RagCitationReference[] = [],
  appendUnreferenced = false,
): CitationRenderModel {
  const safeText = hideDanglingDocToken(text);
  const numberByKey = new Map<string, number>();
  const citationByKey = new Map<string, RagCitationReference>();
  const ordered: Array<{ key: string; number: number; citation: RagCitationReference }> = [];

  const ensureNumber = (citation: RagCitationReference): { key: string; number: number } => {
    const key = citationKey(citation);
    citationByKey.set(key, citation);
    const existing = numberByKey.get(key);
    if (existing) return { key, number: existing };
    const number = numberByKey.size + 1;
    numberByKey.set(key, number);
    ordered.push({ key, number, citation });
    return { key, number };
  };

  citations.forEach(ensureNumber);

  const parts: CitationPart[] = [];
  let cursor = 0;
  let hasInlineCitation = false;
  for (const match of safeText.matchAll(DOC_TOKEN_RE)) {
    const raw = match[0];
    const start = match.index ?? 0;
    const tokenChunkId = match[1];
    const page = match[2];
    if (start > cursor) {
      parts.push({ type: "text", text: safeText.slice(cursor, start) });
    }
    const citation = findCitation(tokenChunkId, citations) ?? fallbackCitation(tokenChunkId, page);
    const { key, number } = ensureNumber(citation);
    parts.push({ type: "citation", key, number, citation });
    hasInlineCitation = true;
    cursor = start + raw.length;
  }
  if (cursor < safeText.length) {
    parts.push({ type: "text", text: safeText.slice(cursor) });
  }
  if (parts.length === 0 && safeText.length > 0) {
    parts.push({ type: "text", text: safeText });
  }

  if (appendUnreferenced && !hasInlineCitation && citations.length > 0) {
    parts.push({ type: "text", text: " " });
    for (const citation of citations.slice(0, 3)) {
      const { key, number } = ensureNumber(citation);
      parts.push({ type: "citation", key, number, citation });
    }
  }

  return {
    parts,
    ordered: ordered.filter((item) => citationByKey.has(item.key)),
  };
}

function hideDanglingDocToken(text: string): string {
  const start = text.lastIndexOf("[doc:");
  if (start < 0) return text;
  const close = text.indexOf("]", start);
  return close >= 0 ? text : text.slice(0, start);
}

function humanTitle(citation: RagCitationReference): string {
  if (citation.doc_title?.trim()) return citation.doc_title.trim();
  if (citation.title?.trim()) return citation.title.trim();
  if (citation.url) {
    try {
      return new URL(citation.url).hostname;
    } catch {
      return "网页来源";
    }
  }
  return "引用来源";
}

function pageLabel(citation: RagCitationReference): string | null {
  if (citation.page === undefined || citation.page === null || citation.page === "") return null;
  return `p${citation.page}`;
}

function sourceLabel(citation: RagCitationReference): string {
  return citation.source ?? citation.kind;
}

function scoreLabel(score: number | undefined): string {
  return typeof score === "number" ? score.toFixed(2) : "无";
}

function citationListLabel(citation: RagCitationReference): string {
  const page = pageLabel(citation);
  return page ? `${humanTitle(citation)} · ${page}` : humanTitle(citation);
}

function CitationPopoverContent({ citation }: { citation: RagCitationReference }): JSX.Element {
  const excerpt = citation.text ?? citation.snippet ?? "原文片段加载中";
  return (
    <div className="max-w-[360px] text-[12px] text-ink-700">
      <div className="font-medium text-ink-900 mb-1">{citationListLabel(citation)}</div>
      <div className="flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-ink-500 mb-2">
        <span>类型：{sourceLabel(citation)}</span>
        <span>score：{scoreLabel(citation.score)}</span>
      </div>
      <div className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded border border-paper-300 bg-paper-50 p-2 leading-5">
        {excerpt}
      </div>
    </div>
  );
}

function CitationBadge({
  number,
  citation,
  active,
  onActiveChange,
}: {
  number: number;
  citation: RagCitationReference;
  active: boolean;
  onActiveChange: (open: boolean) => void;
}): JSX.Element {
  return (
    <Popover
      trigger={["hover", "click"]}
      open={active}
      onOpenChange={onActiveChange}
      content={<CitationPopoverContent citation={citation} />}
      placement="top"
    >
      <button
        type="button"
        className={`mx-0.5 inline-flex h-4 min-w-4 align-super items-center justify-center rounded-full border px-1 text-[10px] leading-none font-semibold transition ${
          active
            ? "border-violet-500 bg-violet-100 text-violet-800 ring-2 ring-violet-200"
            : "border-violet-300 bg-white text-violet-700 hover:bg-violet-100"
        }`}
        aria-label={`查看引用 ${number}：${citationListLabel(citation)}`}
        data-testid={`citation-badge-${number}`}
      >
        {number}
      </button>
    </Popover>
  );
}

export function CitationText({
  text,
  citations = [],
  appendUnreferenced = false,
  activeKey,
  onActiveKeyChange,
}: CitationTextProps): JSX.Element {
  const model = buildCitationModel(text, citations, appendUnreferenced);

  return (
    <>
      {model.parts.map((part, idx) => {
        if (part.type === "text") {
          return <span key={`t-${idx}`}>{part.text}</span>;
        }
        return (
          <CitationBadge
            key={`c-${part.key}-${idx}`}
            number={part.number}
            citation={part.citation}
            active={activeKey === part.key}
            onActiveChange={(open) => onActiveKeyChange(open ? part.key : null)}
          />
        );
      })}
    </>
  );
}

export function CitationList({
  citations = [],
  activeKey,
  onActiveKeyChange,
}: {
  citations?: RagCitationReference[];
  activeKey: string | null;
  onActiveKeyChange: (key: string | null) => void;
}): JSX.Element | null {
  const model = buildCitationModel("", citations, false);
  if (model.ordered.length === 0) return null;

  return (
    <div className="mt-1.5 pt-1.5 border-t border-violet-200/60 text-[11px] text-ink-600 flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className="text-ink-500">引用：</span>
      {model.ordered.slice(0, 8).map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onActiveKeyChange(item.key)}
          className={`rounded px-1.5 py-0.5 transition ${
            activeKey === item.key
              ? "bg-violet-100 text-violet-800 ring-1 ring-violet-300"
              : "bg-white/70 text-ink-700 hover:bg-violet-50"
          }`}
          data-testid={`citation-list-item-${item.number}`}
        >
          {item.number} {citationListLabel(item.citation)}
        </button>
      ))}
    </div>
  );
}

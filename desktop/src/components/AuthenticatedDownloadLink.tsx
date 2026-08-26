import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import { message } from "antd";
import { useBackendOriginFence } from "@/hooks/useBackendOriginFence";
import { apiTransport } from "@/session";

const AUTHENTICATED_DOWNLOAD_MAX_BYTES = 128 * 1024 * 1024;
const OBJECT_URL_GRACE_MS = 30_000;

interface AuthenticatedDownloadLinkProps {
  url: string;
  downloadName?: string;
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
  testId?: string;
  stopPropagation?: boolean;
}

export default function AuthenticatedDownloadLink({
  url,
  downloadName,
  children,
  className,
  ariaLabel,
  testId,
  stopPropagation = false,
}: AuthenticatedDownloadLinkProps): JSX.Element {
  const {
    revision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  } = useBackendOriginFence();
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const objectUrls = useRef<Map<string, number>>(new Map());
  const timers = useRef<Set<number>>(new Set());

  const releaseObjectUrl = useCallback((objectUrl: string): void => {
    const refs = objectUrls.current.get(objectUrl) ?? 0;
    if (refs > 1) {
      objectUrls.current.set(objectUrl, refs - 1);
      return;
    }
    if (refs === 1) {
      objectUrls.current.delete(objectUrl);
      URL.revokeObjectURL(objectUrl);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const activeUrls = objectUrls.current;
    const activeTimers = timers.current;
    return () => {
      for (const timer of activeTimers) clearTimeout(timer);
      activeTimers.clear();
      for (const objectUrl of activeUrls.keys()) URL.revokeObjectURL(objectUrl);
      activeUrls.clear();
    };
  }, [revision]);

  const handleClick = async (event: MouseEvent<HTMLButtonElement>): Promise<void> => {
    if (stopPropagation) event.stopPropagation();
    if (!url || busy) return;
    const generation = captureGeneration();
    const controller = new AbortController();
    const unregisterController = registerAbortController(controller);
    setBusy(true);
    try {
      const response = await apiTransport(
        url,
        { signal: controller.signal },
        {
          timeoutMs: 120_000,
          maxResponseBytes: AUTHENTICATED_DOWNLOAD_MAX_BYTES,
          throwHttpErrors: false,
        },
      );
      if (!response.ok) {
        await response.body?.cancel().catch(() => undefined);
        throw new Error(`artifact download HTTP ${response.status}`);
      }
      const blob = await response.blob();
      if (!mounted.current || controller.signal.aborted || !isCurrent(generation)) return;
      const objectUrl = URL.createObjectURL(blob);
      objectUrls.current.set(objectUrl, (objectUrls.current.get(objectUrl) ?? 0) + 1);
      const bridge = window.echo;
      if (bridge?.isElectron === true) {
        const downloadArtifactBlob = bridge.downloadArtifactBlob;
        if (typeof downloadArtifactBlob !== "function") {
          releaseObjectUrl(objectUrl);
          throw new Error("artifact download bridge unavailable");
        }
        try {
          const result = await downloadArtifactBlob(objectUrl, downloadName);
          if (result.cancelled) return;
          if (!result.ok) throw new Error("artifact download did not complete");
          return;
        } finally {
          releaseObjectUrl(objectUrl);
        }
      }
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = downloadName || "";
      anchor.rel = "noreferrer";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      const timer = window.setTimeout(() => {
        timers.current.delete(timer);
        releaseObjectUrl(objectUrl);
      }, OBJECT_URL_GRACE_MS);
      timers.current.add(timer);
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && isCurrent(generation)) {
        console.error("[artifact-download] authenticated download failed", error);
        void message.error("产物下载失败，请稍后重试");
      }
    } finally {
      unregisterController();
      if (mounted.current && isCurrent(generation)) setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={(event) => void handleClick(event)}
      disabled={busy || !url}
      aria-label={ariaLabel}
      aria-busy={busy}
      data-testid={testId}
      className={className}
    >
      {children}
    </button>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { BACKEND_ORIGIN_EVENT } from "@/runtime";

/**
 * Backend-scoped async work must never commit after the configured backend
 * changes.  This hook provides both a synchronous generation fence and an
 * AbortController registry.  The event handler advances the fence before
 * React schedules the next render, so even a response that resolves in the
 * same turn as an A -> B switch is rejected by the caller.
 */
export function useBackendOriginFence(): {
  revision: number;
  captureGeneration(): number;
  isCurrent(generation: number): boolean;
  registerAbortController(controller: AbortController): () => void;
} {
  const generationRef = useRef(0);
  const controllersRef = useRef<Set<AbortController>>(new Set());
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controllers = controllersRef.current;
    const handleOriginChange = (): void => {
      generationRef.current += 1;
      for (const controller of controllers) controller.abort();
      controllers.clear();
      setRevision((current) => current + 1);
    };
    window.addEventListener(BACKEND_ORIGIN_EVENT, handleOriginChange);
    return () => {
      window.removeEventListener(BACKEND_ORIGIN_EVENT, handleOriginChange);
      for (const controller of controllers) controller.abort();
      controllers.clear();
    };
  }, []);

  const captureGeneration = useCallback((): number => generationRef.current, []);
  const isCurrent = useCallback(
    (generation: number): boolean => generation === generationRef.current,
    [],
  );
  const registerAbortController = useCallback(
    (controller: AbortController): (() => void) => {
      controllersRef.current.add(controller);
      return () => {
        controllersRef.current.delete(controller);
        controller.abort();
      };
    },
    [],
  );

  return {
    revision,
    captureGeneration,
    isCurrent,
    registerAbortController,
  };
}

/**
 * useOnboarding：first-run 体验状态管理（P3.1）。
 *
 * 行为：
 * - 启动时读 localStorage("echodesk.onboarding.completed") 判断是否首次打开
 * - 暴露 markCompleted() 让 Modal 收到"完成"或"跳过"后写持久化标志
 * - 暴露 resetForDebug() 让设置面板"重新看一次引导"能触发
 *
 * 写在 hook 而不是 store 的原因：onboarding 是一次性的非业务状态，
 * 不需要进 store.ts 的事件流；用最薄的 React state + localStorage 即可。
 */

import { useCallback, useEffect, useState } from "react";
import { isTvLikeViewport } from "@/runtime";

const STORAGE_KEY = "echodesk.onboarding.completed";

function loadCompleted(): boolean {
  if (typeof window === "undefined") return true;
  if (isTvLikeViewport()) return true;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return true; // localStorage 不可用时不弹（避免每次启动都弹）
  }
}

function persistCompleted(v: boolean): void {
  try {
    if (v) {
      window.localStorage.setItem(STORAGE_KEY, "1");
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    /* ignore */
  }
}

export interface OnboardingState {
  /** 是否应该显示引导（首次启动 + 未完成）。 */
  shouldShow: boolean;
  /** Modal 内点击"完成"或"跳过"调用，立即关闭并持久化。 */
  markCompleted: () => void;
  /** 设置面板的"重新看一次"按钮调用，清掉标志让下次启动重弹。 */
  resetForDebug: () => void;
}

export function useOnboarding(): OnboardingState {
  const [shouldShow, setShouldShow] = useState<boolean>(() => !loadCompleted());

  // TV / Android public demo should be usable immediately with a remote. The
  // full desktop onboarding modal can be too tall on landscape TV WebViews.
  useEffect(() => {
    const bypassForTv = () => {
      if (!isTvLikeViewport()) return;
      persistCompleted(true);
      setShouldShow(false);
    };
    bypassForTv();
    window.addEventListener("resize", bypassForTv, { passive: true });
    window.addEventListener("orientationchange", bypassForTv, { passive: true });
    return () => {
      window.removeEventListener("resize", bypassForTv);
      window.removeEventListener("orientationchange", bypassForTv);
    };
  }, []);

  // 兼容 storage 在其他 tab 被改的场景（dev 调试方便）
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) {
        setShouldShow(!loadCompleted());
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const markCompleted = useCallback(() => {
    persistCompleted(true);
    setShouldShow(false);
  }, []);

  const resetForDebug = useCallback(() => {
    if (isTvLikeViewport()) {
      persistCompleted(true);
      setShouldShow(false);
      return;
    }
    persistCompleted(false);
    setShouldShow(true);
  }, []);

  return { shouldShow, markCompleted, resetForDebug };
}

export const PRODUCT_MODEL_NAME = "DeepSeek V4 Flash";
export const SMALL_MODEL_NAME = "Qwen3 8B";

/** Keep provider, route and deployment identifiers inside runtime contracts. */
export function modelDisplayName(runtimeName?: unknown): string {
  if (typeof runtimeName === "string") {
    const normalized = runtimeName.trim().toLowerCase();
    if (
      normalized.includes("gpt-5.4-nano") ||
      normalized.includes("qwen") ||
      normalized.includes("fast") ||
      normalized.includes("memory")
    ) {
      return SMALL_MODEL_NAME;
    }
  }
  return PRODUCT_MODEL_NAME;
}

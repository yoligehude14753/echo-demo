export const PRODUCT_MODEL_NAME = "DeepSeek V4 Flash";
export const SMALL_MODEL_NAME = "Qwen3 8B";

/** Keep provider, route and deployment identifiers inside runtime contracts. */
export function modelDisplayName(runtimeName?: unknown): string {
  const name = String(runtimeName ?? "").toLowerCase();
  if (name.includes("qwen") || name.includes("nano") || name.includes("fast_route")) {
    return SMALL_MODEL_NAME;
  }
  return PRODUCT_MODEL_NAME;
}

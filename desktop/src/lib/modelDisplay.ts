export const PRODUCT_MODEL_NAME = "Qwen3-B8";

/** Keep provider, route and deployment identifiers inside runtime contracts. */
export function modelDisplayName(_runtimeName?: unknown): string {
  return PRODUCT_MODEL_NAME;
}
